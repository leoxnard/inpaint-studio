import asyncio
import hashlib

import httpx
import pytest

import installer

SHA = hashlib.sha256(b"hello").hexdigest()


def test_file_sha256(tmp_path):
    data = b"x" * (9 << 20)
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    assert installer.file_sha256(f) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("headers,want", [
    ({"x-linked-etag": f'"{SHA}"'}, SHA),
    ({"X-Linked-ETag": SHA}, SHA),
    ({"etag": f'W/"{SHA}"'}, SHA),
    ({"etag": '"abc123"'}, None),
    ({"x-linked-etag": "nothex" * 11}, None),
    ({}, None),
])
def test_expected_sha256(headers, want):
    assert installer.expected_sha256([httpx.Response(302, headers=headers)]) == want


def test_expected_sha256_from_redirect():
    redirect = httpx.Response(302, headers={"x-linked-etag": f'"{SHA}"'})
    assert installer.expected_sha256([redirect, httpx.Response(200)]) == SHA


def test_verify_download(tmp_path):
    part = tmp_path / "m.safetensors.part"
    part.write_bytes(b"hello")
    asyncio.run(installer.verify_download(part, SHA))
    asyncio.run(installer.verify_download(part, None))
    assert part.exists()
    with pytest.raises(RuntimeError, match="m.safetensors: checksum mismatch"):
        asyncio.run(installer.verify_download(part, "0" * 64))
    assert not part.exists()
