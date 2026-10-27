"""Compress large payloads before they reach Temporal.

A source analysis sends up to a few megabytes of repository evidence, and
Temporal refuses any single payload over 2 MB. That evidence is JSON, which
zlib shrinks several times over, and it is stored again in every model
activity's input — so everything over a small threshold is compressed, on the
client (the API) and on the worker alike. Both sides must use the same codec.
"""

from __future__ import annotations

import zlib
from typing import List, Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

ENCODING = b"binary/zlib"
_THRESHOLD = 32 * 1024


class ZlibCodec(PayloadCodec):
    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        out = []
        for payload in payloads:
            raw = payload.SerializeToString()
            if len(raw) < _THRESHOLD:
                out.append(payload)
                continue
            out.append(Payload(metadata={"encoding": ENCODING}, data=zlib.compress(raw, 6)))
        return out

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        out = []
        for payload in payloads:
            if payload.metadata.get("encoding") != ENCODING:
                out.append(payload)
                continue
            decoded = Payload()
            decoded.ParseFromString(zlib.decompress(payload.data))
            out.append(decoded)
        return out


def data_converter() -> DataConverter:
    # PydanticAIPlugin swaps in its own payload converter and keeps this codec.
    return DataConverter(payload_codec=ZlibCodec())
