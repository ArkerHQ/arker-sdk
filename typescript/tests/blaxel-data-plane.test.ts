import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ResponseError } from '@blaxel/core';
import { createServer } from 'node:http';
import { connect } from 'node:net';
import { createBlaxelDataPlane } from '../src/compat/blaxel-data-plane.js';
import { listen, closeServer } from './provider-fixtures.js';

test('native filesystem uses only the scoped port transport and rejects redirects', async t => {
  let requests = 0;
  const server = createServer((req, res) => {
    requests++;
    assert.equal(req.headers.authorization, undefined);
    assert.equal(req.headers['x-api-key'], undefined);
    if (req.url?.includes('missing')) {
      res.writeHead(404, {'content-type':'application/json'}); res.end(JSON.stringify({error:'missing'}));
    } else if (req.url?.includes('redirect')) {
      res.writeHead(307, {location:'https://custom.example/stolen'}); res.end();
    } else { res.writeHead(200, {'content-type':'application/json'}); res.end(JSON.stringify({content:'native'})); }
  });
  t.after(() => closeServer(server));
  const address = await listen(server);
  const plane = createBlaxelDataPlane({connectPort: async port => {
    assert.equal(port,49983);
    return connect(address.port,'127.0.0.1');
  }});
  t.after(() => plane.close());
  assert.equal(await plane.fs.read('/file'),'native');
  await assert.rejects(plane.fs.read('/redirect'));
  assert.equal(requests,2);
  await assert.rejects(plane.fs.read('/missing'), (error: any) => {
    assert(error instanceof ResponseError);
    assert.equal(error.status, 404);
    assert(error.response.url.includes('missing'));
    return true;
  });
  plane.close();
  await assert.rejects(plane.fs.read('/closed'),/closed/);
  assert.equal(requests,3);
});

test('closing the plane aborts an active native read', async t => {
  let started!: () => void;
  const reading = new Promise<void>(resolve => { started = resolve; });
  const server = createServer((_req, res) => {
    res.writeHead(200, {'content-type':'application/json'});
    res.write('{"content":"');
    started();
  });
  t.after(() => closeServer(server));
  const address = await listen(server);
  const plane = createBlaxelDataPlane({connectPort: async () => connect(address.port, '127.0.0.1')});
  t.after(() => plane.close());
  const result = assert.rejects(plane.fs.read('/slow'));
  await reading;
  plane.close();
  await result;
});

test('native watch reports keepalive, trailing event, and remote close through the private transport', {timeout: 5000}, async t => {
  const server = createServer((_req, res) => {
    res.writeHead(200, {'content-type': 'application/x-ndjson'});
    res.end('[keepalive]\n' + JSON.stringify({op: 'WRITE', path: '/tmp', name: 'tail'}));
  });
  t.after(() => closeServer(server));
  const address = await listen(server);
  const plane = createBlaxelDataPlane({connectPort: async port => {
    assert.equal(port, 49983);
    return connect(address.port, '127.0.0.1');
  }});
  t.after(() => plane.close());
  const events: string[] = [];
  let keepalives = 0;
  let closed!: () => void;
  let failed!: (error: Error) => void;
  const completion = new Promise<void>((resolve, reject) => { closed = resolve; failed = reject; });
  const handle = plane.fs.watch('/tmp', event => { events.push(event.name); }, {
    withContent: false, onKeepalive: () => { keepalives++; }, onClose: closed, onError: failed,
  });
  t.after(() => handle.close());
  await completion;
  assert.deepEqual(events, ['tail']);
  assert.equal(keepalives, 1);
});
