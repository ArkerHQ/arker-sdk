"""Owned live URL cases shared by native and routed sync/async interfaces."""

import asyncio
import json
import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from e2b import Sandbox
import asyncio
import hashlib
import inspect
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import httpx

async def result(value):
    return await value if inspect.isawaitable(value) else value

async def program():
    sandbox = await result(Sandbox.create())
    try:
        prefix = '/tmp/url-' + uuid.uuid4().hex
        path = prefix + '-file'
        payload = bytes(range(256)) * 32768
        def mint(method, *args, **kwargs):
            before = time.perf_counter()
            url = method(*args, **kwargs)
            elapsed = time.perf_counter() - before
            assert isinstance(url, str), 'Native URL methods are synchronous in both interfaces'
            # This measures blocking call duration, not event-loop responsiveness.
            measurements.append(elapsed)
            return url

        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            if case == 'multi-file':
                url = mint(sandbox.upload_url, '')
                files = [('file', (prefix + '-a', b'first')), ('file', (prefix + '-b', b'second'))]
                response = await client.post(url, files=files)
                assert response.status_code == 200, response.status_code
                assert [entry['path'] for entry in response.json()] == [prefix + '-a', prefix + '-b']
                assert bytes(await result(sandbox.files.read(prefix + '-a', format='bytes'))) == b'first'
                assert bytes(await result(sandbox.files.read(prefix + '-b', format='bytes'))) == b'second'
            elif case == 'custom-user':
                username = 'url_' + uuid.uuid4().hex[:12]
                home = '/home/' + username
                await result(sandbox.commands.run('useradd -m -d ' + home + ' ' + username, user='root'))
                url = mint(sandbox.upload_url, '~/owned', user=username)
                response = await client.post(url, files={'file': ('ignored', b'owned')})
                assert response.status_code == 200, response.status_code
                info = await result(sandbox.files.get_info(home + '/owned'))
                assert info.owner == username
                await result(sandbox.commands.run('chmod 000 ' + home + '/owned', user='root'))
                response = await client.get(mint(sandbox.download_url, home + '/owned', user='user'))
                assert response.status_code == 200 and response.content == b'owned'
            elif case == 'cancel-upload':
                prefix_bytes = payload[:1024 * 1024]
                wait_forever = asyncio.Event()
                async def partial_multipart():
                    yield (b'--upload-boundary\r\n'
                           b'Content-Disposition: form-data; name="file"; filename="ignored"\r\n\r\n')
                    yield prefix_bytes
                    await wait_forever.wait()
                url = mint(sandbox.upload_url, path)
                uploading = asyncio.create_task(client.post(url, content=partial_multipart(),
                    headers={'Content-Type': 'multipart/form-data; boundary=upload-boundary'}))
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        assert not uploading.done(), 'Upload ended before cancellation'
                        if await result(sandbox.files.exists(path)):
                            info = await result(sandbox.files.get_info(path))
                            if info.size >= len(prefix_bytes) // 2:
                                break
                        assert time.monotonic() < deadline, 'Native upload did not write before body completion'
                        await asyncio.sleep(0)
                finally:
                    uploading.cancel()
                    try:
                        await uploading
                    except asyncio.CancelledError:
                        pass
                partial = bytes(await result(sandbox.files.read(path, format='bytes')))
                # Cancellation races buffered writes; every persisted byte must
                # be the transmitted prefix, and the observed write must remain.
                assert len(prefix_bytes) // 2 <= len(partial) <= len(prefix_bytes)
                assert partial == prefix_bytes[:len(partial)]
            elif case == 'round-trip':
                response = await client.post(mint(sandbox.upload_url, path), files={'file': ('ignored', payload)})
                assert response.status_code == 200, response.status_code
                assert response.json()[0]['path'] == path
                response = await client.get(mint(sandbox.download_url, path))
                assert response.status_code == 200, response.status_code
                assert len(response.content) == len(payload)
                assert hashlib.sha256(response.content).digest() == hashlib.sha256(payload).digest()
            else:
                await result(sandbox.files.write(path, payload))
                if case == 'expiry':
                    url = mint(sandbox.download_url, path, use_signature_expiration=0)
                    deadline = time.monotonic() + 5
                    while True:
                        response = await client.get(url)
                        if response.status_code == 401:
                            break
                        assert response.status_code == 200, response.status_code
                        assert time.monotonic() < deadline, 'Zero-second signature outlived its Unix second'
                    response = await client.get(mint(sandbox.download_url, path))
                    assert response.status_code == 200 and response.content == payload
                elif case in ('wrong-path', 'wrong-user', 'wrong-expiry'):
                    url = mint(sandbox.download_url, path)
                    parts = urlsplit(url)
                    fields = dict(parse_qsl(parts.query))
                    name, value = {
                        'wrong-path': ('path', prefix + '-other'),
                        'wrong-user': ('username', 'root'),
                        'wrong-expiry': ('signature_expiration', '4294967295'),
                    }[case]
                    fields[name] = value
                    changed = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(fields), parts.fragment))
                    response = await client.get(changed)
                    assert response.status_code == 401, response.status_code
                    response = await client.get(url)
                    assert response.status_code == 200 and response.content == payload
                elif case == 'range':
                    response = await client.get(mint(sandbox.download_url, path),
                        headers={'Range': 'bytes=100-199', 'Accept-Encoding': 'identity'})
                    assert response.status_code == 206, response.status_code
                    assert response.headers['content-range'] == f'bytes 100-199/{len(payload)}'
                    assert response.content == payload[100:200]
                elif case == 'cancel-download':
                    url = mint(sandbox.download_url, path)
                    async with client.stream('GET', url, headers={'Accept-Encoding': 'identity'}) as response:
                        assert response.status_code == 200, response.status_code
                        chunk = await anext(response.aiter_bytes())
                        assert chunk and len(chunk) < len(payload) and payload.startswith(chunk)
                    response = await client.get(url)
                    assert response.status_code == 200 and response.content == payload
                else:
                    raise AssertionError(case)
    finally:
        assert await result(sandbox.kill()) is True
"""

CASES = [
    "round-trip",
    "multi-file",
    "custom-user",
    "range",
    "cancel-download",
    "cancel-upload",
    "wrong-path",
    "wrong-user",
    "wrong-expiry",
    "expiry",
]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_url_differential(case, style, provider, shim, live_router, monkeypatch, record_property):
    apis = ["Sandbox.create", "Sandbox.kill", "Sandbox.download_url", "Sandbox.upload_url"]
    with record_scenario("e2b", style, provider, shim, "import-only-url-" + case, apis):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID")), (
            "An isolated source is required"
        )
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        source = PROGRAM
        if style == "async":
            source = source.replace("from e2b import Sandbox", "from e2b import AsyncSandbox as Sandbox")
        if shim:
            source = source.replace("from e2b import", "from arker.e2b import")
        measurements = []
        namespace = {"case": case, "measurements": measurements}
        try:
            exec(compile(source, __file__, "exec"), namespace)
            asyncio.run(namespace["program"]())
        finally:
            record_property("url_mint_seconds", json.dumps(measurements))
