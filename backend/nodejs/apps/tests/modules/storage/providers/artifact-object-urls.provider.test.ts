import 'reflect-metadata'
import { expect } from 'chai'
import sinon from 'sinon'
import fs from 'fs'
import os from 'os'
import path from 'path'
import AmazonS3Adapter from '../../../../src/modules/storage/providers/s3.provider'
import AzureBlobStorageAdapter from '../../../../src/modules/storage/providers/azure.provider'
import LocalStorageAdapter from '../../../../src/modules/storage/providers/local-storage.provider'
import { PresignedUrlError, StorageValidationError } from '../../../../src/libs/errors/storage.errors'

const KEY = 'org1/PipesHub/records/vr-1/0123456789abcdef/blocks/part-0000.jsonl'

describe('Signed object URLs for artifact sets', () => {
  afterEach(() => sinon.restore())

  describe('S3', () => {
    // Real SigV4 presigning: it needs credentials but no network.
    const adapter = () =>
      new AmazonS3Adapter({
        accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
        secretAccessKey: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        region: 'us-east-1',
        bucket: 'my-bucket',
      })

    it('signs a PUT for the key and tells the uploader which Content-Type to store', async () => {
      const signed = await adapter().signObjectUrl(KEY, {
        method: 'PUT',
        contentType: 'application/x-ndjson',
        expiresInSeconds: 900,
      })

      const url = new URL(signed.url)
      expect(url.pathname).to.contain(KEY)
      expect(url.searchParams.get('X-Amz-Expires')).to.equal('900')
      expect(url.searchParams.get('x-id')).to.equal('PutObject')
      expect(signed.headers).to.deep.equal({ 'Content-Type': 'application/x-ndjson' })
    })

    it('signs a GET with no headers to send', async () => {
      const signed = await adapter().signObjectUrl(KEY, { method: 'GET', expiresInSeconds: 900 })

      expect(new URL(signed.url).searchParams.get('X-Amz-Expires')).to.equal('900')
      expect(signed.headers).to.deep.equal({})
    })

    it('wraps a signing failure in PresignedUrlError', async () => {
      const a = adapter()
      sinon.stub((a as any).s3, 'getSignedUrlPromise').rejects(new Error('boom'))

      try {
        await a.signObjectUrl(KEY, { method: 'GET', expiresInSeconds: 900 })
        expect.fail('Should have thrown')
      } catch (error) {
        expect(error).to.be.instanceOf(PresignedUrlError)
      }
    })
  })

  describe('Azure', () => {
    const adapterWith = (blobClient: { generateSasUrl: sinon.SinonStub }) => {
      const a = Object.create(AzureBlobStorageAdapter.prototype)
      a.containerReady = Promise.resolve()
      a.containerClient = { getBlockBlobClient: sinon.stub().returns(blobClient) }
      return a as AzureBlobStorageAdapter
    }

    it('signs a PUT with create and write rights, and names the blob type Azure requires', async () => {
      const blobClient = { generateSasUrl: sinon.stub().resolves('https://acct.blob/c/k?sas') }
      const before = Date.now()

      const signed = await adapterWith(blobClient).signObjectUrl(KEY, {
        method: 'PUT',
        contentType: 'image/png',
        expiresInSeconds: 900,
      })

      const opts = blobClient.generateSasUrl.firstCall.args[0]
      expect(opts.permissions.toString()).to.equal('cw')
      expect(opts.expiresOn.getTime() - before).to.be.within(899_000, 901_000)
      expect(signed).to.deep.equal({
        url: 'https://acct.blob/c/k?sas',
        headers: { 'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'image/png' },
      })
    })

    it('signs a GET with read rights only', async () => {
      const blobClient = { generateSasUrl: sinon.stub().resolves('https://acct.blob/c/k?sas') }

      const signed = await adapterWith(blobClient).signObjectUrl(KEY, { method: 'GET', expiresInSeconds: 900 })

      expect(blobClient.generateSasUrl.firstCall.args[0].permissions.toString()).to.equal('r')
      expect(signed.headers).to.deep.equal({})
    })

    it('wraps a signing failure in PresignedUrlError', async () => {
      const blobClient = { generateSasUrl: sinon.stub().rejects(new Error('no key')) }

      try {
        await adapterWith(blobClient).signObjectUrl(KEY, { method: 'GET', expiresInSeconds: 900 })
        expect.fail('Should have thrown')
      } catch (error) {
        expect(error).to.be.instanceOf(PresignedUrlError)
      }
    })
  })

  describe('local', () => {
    let home: string

    beforeEach(() => {
      home = fs.mkdtempSync(path.join(os.tmpdir(), 'artifact-local-'))
      sinon.stub(os, 'homedir').returns(home)
    })

    afterEach(() => fs.rmSync(home, { recursive: true, force: true }))

    const adapter = () =>
      new LocalStorageAdapter({ mountName: 'PipesHub', baseUrl: 'http://localhost:3000' } as any)

    it('writes a file at its key, creating its folders, and replaces it whole', async () => {
      const a = adapter()
      await a.writeObject(KEY, Buffer.from('first'))
      await a.writeObject(KEY, Buffer.from('second'))

      const full = (await a.localObjectPath(KEY))!
      expect(full.startsWith(fs.realpathSync(home))).to.be.true
      expect(fs.readFileSync(full, 'utf8')).to.equal('second')
      // The temporary file the atomic write used is gone.
      expect(fs.readdirSync(path.dirname(full))).to.deep.equal(['part-0000.jsonl'])
    })

    it('refuses a key that would leave the mount', async () => {
      for (const key of ['../escape.json', '/etc/passwd', 'a/../../b']) {
        try {
          await adapter().localObjectPath(key)
          expect.fail(key)
        } catch (error) {
          expect(error, key).to.be.instanceOf(StorageValidationError)
        }
      }
    })

    it('does not follow a symlinked folder out of the mount, for a write or a read', async () => {
      const a = adapter()
      await a.writeObject(KEY, Buffer.from('inside'))
      const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'artifact-outside-'))
      try {
        fs.writeFileSync(path.join(outside, 'part-0000.jsonl'), 'secret')
        const blocks = path.dirname((await a.localObjectPath(KEY))!)
        fs.rmSync(blocks, { recursive: true })
        fs.symlinkSync(outside, blocks)

        for (const attempt of [() => a.writeObject(KEY, Buffer.from('escaped')), () => a.localObjectPath(KEY)]) {
          try {
            await attempt()
            expect.fail('followed the link')
          } catch (error) {
            expect(error).to.be.instanceOf(StorageValidationError)
          }
        }
        expect(fs.readFileSync(path.join(outside, 'part-0000.jsonl'), 'utf8')).to.equal('secret')
      } finally {
        fs.rmSync(outside, { recursive: true, force: true })
      }
    })

    it('answers null for a key whose folder does not exist', async () => {
      expect(await adapter().localObjectPath('org/PipesHub/records/vr/abcdef12/record.json')).to.equal(null)
    })
  })
})
