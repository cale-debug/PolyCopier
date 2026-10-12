// libsodium-compatible sealed box (crypto_box_seal), which GitHub requires for
// repository secrets. Built on tweetnacl + blakejs so the page needs no build step.
// nonce = blake2b(ephemeral_pk || recipient_pk, 24); out = ephemeral_pk || box(msg)
(function (root) {
  function seal(message, recipientPk) {
    const nacl = root.nacl, blake = root.blakejs;
    const eph = nacl.box.keyPair();
    const nonceIn = new Uint8Array(64);
    nonceIn.set(eph.publicKey, 0);
    nonceIn.set(recipientPk, 32);
    const nonce = blake.blake2b(nonceIn, null, 24);
    const boxed = nacl.box(message, nonce, recipientPk, eph.secretKey);
    const out = new Uint8Array(32 + boxed.length);
    out.set(eph.publicKey, 0);
    out.set(boxed, 32);
    return out;
  }
  root.sealBox = seal;
  if (typeof module !== "undefined") module.exports = seal;
})(typeof window !== "undefined" ? window : globalThis);
