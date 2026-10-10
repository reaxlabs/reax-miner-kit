"""The 32-byte public-key SS58 form with Bittensor prefix 42."""
from __future__ import annotations

import hashlib

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def encode(public_key):
    if isinstance(public_key, str):
        public_key = bytes.fromhex(public_key[2:] if public_key.startswith('0x') else public_key)
    if len(public_key) != 32:
        raise ValueError('public key must be 32 bytes')
    payload = bytes([42]) + public_key
    raw = payload + hashlib.blake2b(b'SS58PRE' + payload).digest()[:2]
    num = int.from_bytes(raw, 'big')
    encoded = ''
    while num:
        num, rem = divmod(num, 58)
        encoded = ALPHABET[rem] + encoded
    return '1' * (len(raw) - len(raw.lstrip(b'\x00'))) + encoded


def decode(address):
    if not isinstance(address, str) or len(address.split()) in (12, 24):
        raise ValueError('Never paste a recovery phrase; supply only a public SS58 address.')
    if len(address) not in (47, 48):
        raise ValueError('invalid public SS58 address')
    num = 0
    for char in address:
        if char not in ALPHABET:
            raise ValueError('invalid public SS58 address')
        num = num * 58 + ALPHABET.index(char)
    raw = b'\x00' * (len(address) - len(address.lstrip('1'))) + num.to_bytes((num.bit_length() + 7) // 8, 'big')
    if len(raw) != 35 or raw[0] != 42:
        raise ValueError('SS58 must contain a 32-byte public key with prefix 42')
    if raw[-2:] != hashlib.blake2b(b'SS58PRE' + raw[:-2]).digest()[:2]:
        raise ValueError('SS58 checksum is invalid')
    return raw[1:33]


def public_key_hex(address):
    return '0x' + decode(address).hex()
