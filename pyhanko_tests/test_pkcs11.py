"""
Tests for PKCS#11 functionality.

NOTE: these are not run in CI, due to lack of testing setup.
"""
import asyncio
import binascii
import logging
import os
from io import BytesIO
from typing import Optional

import pytest
from asn1crypto import algos
from asn1crypto.algos import SignedDigestAlgorithm
from certomancer.registry import CertLabel
from freezegun import freeze_time
from pkcs11 import Mechanism, NoSuchKey, PKCS11Error
from pkcs11 import types as p11_types
from pyhanko_certvalidator.registry import SimpleCertificateStore

from pyhanko.config import PKCS11SignatureConfig
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import general, pkcs11, signers
from pyhanko.sign.general import SigningError
from pyhanko.sign.pkcs11 import PKCS11SigningContext, find_token
from pyhanko_tests.samples import MINIMAL, TESTING_CA
from pyhanko_tests.signing_commons import (
    SIMPLE_DSA_V_CONTEXT,
    SIMPLE_ECC_V_CONTEXT,
    async_val_trusted,
    val_trusted,
)

logger = logging.getLogger(__name__)

SKIP_PKCS11 = False
pkcs11_test_module = os.environ.get('PKCS11_TEST_MODULE', None)
if not pkcs11_test_module:
    logger.warning("Skipping PKCS#11 tests --- no PCKS#11 module specified")
    SKIP_PKCS11 = True


def _simple_sess(token='testrsa'):
    return pkcs11.open_pkcs11_session(
        pkcs11_test_module, user_pin='1234', token_label=token
    )


default_other_certs = ('root', 'interm')
SIGNER_LABEL = 'signer1'


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch,pss', [(True, True), (False, False),
                                            (True, False), (True, True)])
@freeze_time('2020-11-01')
def test_simple_sign(bulk_fetch, pss):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch, prefer_pss=pss
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_sign_external_certs(bulk_fetch):
    # Test to see if unnecessary fetches for intermediate certs are skipped

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL,
            ca_chain=(TESTING_CA.get_cert(CertLabel('interm')),),
            bulk_fetch=bulk_fetch
        )
        orig_fetcher = pkcs11._pull_cert
        try:
            def _trap_pull(session, *, label=None, cert_id=None):
                if label != SIGNER_LABEL:
                    raise RuntimeError
                return orig_fetcher(session, label=label, cert_id=cert_id)

            pkcs11._pull_cert = _trap_pull
            assert isinstance(signer.cert_registry, SimpleCertificateStore)
            assert len(list(signer.cert_registry)) == 1
            out = signers.sign_pdf(w, meta, signer=signer)
        finally:
            pkcs11._pull_cert = orig_fetcher

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_sign_multiple_cert_sources(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=('root',),
            ca_chain=(TESTING_CA.get_cert(CertLabel('interm')),),
            bulk_fetch=bulk_fetch
        )
        assert isinstance(signer.cert_registry, SimpleCertificateStore)
        assert len(list(signer.cert_registry)) == 2
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_wrong_key_label(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch, key_label='NoSuchKeyExists'
        )
        with pytest.raises(NoSuchKey):
            signers.sign_pdf(w, meta, signer=signer)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_wrong_cert(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, key_label=SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch, cert_id=binascii.unhexlify(b'deadbeef')
        )
        with pytest.raises(PKCS11Error, match='Could not find.*with ID'):
            signers.sign_pdf(w, meta, signer=signer)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@freeze_time('2020-11-01')
def test_provided_certs():

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    signer_cert = TESTING_CA.get_cert(CertLabel('signer1'))
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, key_label=SIGNER_LABEL,
            signing_cert=signer_cert,
            ca_chain={
                TESTING_CA.get_cert(CertLabel('root')),
                TESTING_CA.get_cert(CertLabel('interm')),
            },
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    assert emb.signer_cert.dump() == signer_cert.dump()
    # this will fail if the intermediate cert is not present
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_signer_provided_others_pulled(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL,
            ca_chain={
                TESTING_CA.get_cert(CertLabel('root')),
                TESTING_CA.get_cert(CertLabel('interm')),
            },
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_signer_pulled_others_provided(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    signer_cert = TESTING_CA.get_cert(CertLabel('signer1'))
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, key_label=SIGNER_LABEL,
            signing_cert=signer_cert, bulk_fetch=bulk_fetch,
            other_certs_to_pull=default_other_certs
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    assert emb.signer_cert.dump() == signer_cert.dump()
    # this will fail if the intermediate cert is not present
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@freeze_time('2020-11-01')
def test_unclear_key_label():
    signer_cert = TESTING_CA.get_cert(CertLabel('signer1'))
    with _simple_sess() as sess:
        with pytest.raises(SigningError, match='\'key_label\'.*must be prov'):
            pkcs11.PKCS11Signer(
                sess, signing_cert=signer_cert,
                other_certs_to_pull=default_other_certs,
            )


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@freeze_time('2020-11-01')
def test_unclear_signer_cert():
    with _simple_sess() as sess:
        with pytest.raises(SigningError, match='Please specify'):
            pkcs11.PKCS11Signer(
                sess, other_certs_to_pull=default_other_certs,
            )


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_simple_sign_dsa(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(
        field_name='Sig1', md_algorithm='sha256'
    )
    with _simple_sess(token='testdsa') as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb, vc=SIMPLE_DSA_V_CONTEXT())


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_simple_sign_ecdsa(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(
        field_name='Sig1', md_algorithm='sha256'
    )
    with _simple_sess(token='testecdsa') as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch, use_raw_mechanism=True
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb, vc=SIMPLE_ECC_V_CONTEXT())


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@freeze_time('2020-11-01')
def test_simple_sign_from_config():

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    config = PKCS11SignatureConfig(
        module_path=pkcs11_test_module, token_label='testrsa',
        cert_label=SIGNER_LABEL, user_pin='1234', other_certs_to_pull=None
    )

    with PKCS11SigningContext(config) as signer:
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@freeze_time('2020-11-01')
def test_simple_sign_with_raw_rsa():

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    config = PKCS11SignatureConfig(
        module_path=pkcs11_test_module, token_label='testrsa',
        cert_label=SIGNER_LABEL, user_pin='1234', other_certs_to_pull=None,
        raw_mechanism=True
    )

    with PKCS11SigningContext(config) as signer:
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb)


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch', [True, False])
@freeze_time('2020-11-01')
def test_simple_sign_with_raw_dsa(bulk_fetch):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(
        field_name='Sig1', md_algorithm='sha256'
    )
    with _simple_sess(token='testdsa') as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch, use_raw_mechanism=True
        )
        out = signers.sign_pdf(w, meta, signer=signer)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    val_trusted(emb, vc=SIMPLE_DSA_V_CONTEXT())


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
def test_no_raw_pss():
    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(
        field_name='Sig1', md_algorithm='sha256'
    )
    with _simple_sess(token='testrsa') as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            use_raw_mechanism=True, prefer_pss=True
        )
        with pytest.raises(NotImplementedError, match='PSS not available'):
            signers.sign_pdf(w, meta, signer=signer)


def test_unsupported_algo():
    with pytest.raises(NotImplementedError, match="2.999"):
        pkcs11.select_pkcs11_signing_params(
            algos.SignedDigestAlgorithm({'algorithm': '2.999'}),
            digest_algorithm='sha256',
            use_raw_mechanism=False
        )


@pytest.mark.parametrize('md', ['sha256', 'sha384'])
def test_select_ecdsa_mech(md):
    # can't do a round-trip test since softhsm doesn't support these, but
    # we can at least verify that the selection works
    algo = f'{md}_ecdsa'
    result = pkcs11.select_pkcs11_signing_params(
        algos.SignedDigestAlgorithm({'algorithm': algo}),
        digest_algorithm=md,
        use_raw_mechanism=False
    )
    assert result.sign_kwargs['mechanism'] \
           == getattr(Mechanism, f"ECDSA_{md.upper()}")


@pytest.mark.parametrize('label,cert_id,no_results,exp_err', [
    ('foo', b'foo', True, "Could not find cert with label 'foo', ID '666f6f'."),
    ('foo', None, True, "Could not find cert with label 'foo'."),
    (None, b'foo', True, "Could not find cert with ID '666f6f'."),
    ('foo', None, False, "Found more than one cert with label 'foo'."),
])
def test_pull_err_fmt(label, cert_id, no_results, exp_err):
    err = pkcs11._format_pull_err_msg(
        no_results=no_results, label=label, cert_id=cert_id
    )
    assert err == exp_err


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.parametrize('bulk_fetch,pss', [(True, True), (False, False),
                                            (True, False), (True, True)])
@freeze_time('2020-11-01')
@pytest.mark.asyncio
async def test_simple_sign_from_config_async(bulk_fetch, pss):

    w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
    meta = signers.PdfSignatureMetadata(field_name='Sig1')
    config = PKCS11SignatureConfig(
        module_path=pkcs11_test_module, token_label='testrsa',
        other_certs_to_pull=default_other_certs,
        bulk_fetch=bulk_fetch, prefer_pss=pss,
        cert_label=SIGNER_LABEL, user_pin='1234'
    )
    async with PKCS11SigningContext(config=config) as signer:
        pdf_signer = signers.PdfSigner(meta, signer)
        out = await pdf_signer.async_sign_pdf(w)

    r = PdfFileReader(out)
    emb = r.embedded_signatures[0]
    assert emb.field_name == 'Sig1'
    await async_val_trusted(emb)


# @pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.skip  # FIXME flaky test, sometimes coredumps with SoftHSM
@pytest.mark.parametrize('bulk_fetch,pss', [(True, True), (False, False),
                                            (True, False), (True, True)])
@pytest.mark.asyncio
async def test_async_sign_many_concurrent(bulk_fetch, pss):

    concurrent_count = 10
    config = PKCS11SignatureConfig(
        module_path=pkcs11_test_module, token_label='testrsa',
        other_certs_to_pull=default_other_certs,
        bulk_fetch=bulk_fetch, prefer_pss=pss,
        cert_label=SIGNER_LABEL, user_pin='1234'
    )
    async with PKCS11SigningContext(config=config) as signer:
        async def _job(_i):
            w = IncrementalPdfFileWriter(BytesIO(MINIMAL))
            meta = signers.PdfSignatureMetadata(
                field_name='Sig1', reason=f"PKCS#11 concurrency test #{_i}!"
            )
            pdf_signer = signers.PdfSigner(meta, signer)
            sig_result = await pdf_signer.async_sign_pdf(w, in_place=True)
            await asyncio.sleep(2)
            return _i, sig_result

        jobs = asyncio.as_completed(map(_job, range(1, concurrent_count + 1)))
        for finished_job in jobs:
            i, out = await finished_job
            r = PdfFileReader(out)
            emb = r.embedded_signatures[0]
            assert emb.field_name == 'Sig1'
            assert emb.sig_object['/Reason'].endswith(f"#{i}!")
            with freeze_time("2020-11-01"):
                await async_val_trusted(emb)


# @pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
@pytest.mark.skip  # FIXME flaky test, sometimes coredumps with SoftHSM
@pytest.mark.parametrize('bulk_fetch,pss', [(True, True), (False, False),
                                            (True, False), (True, True)])
@pytest.mark.asyncio
async def test_async_sign_raw_many_concurrent_no_preload_objs(bulk_fetch, pss):
    concurrent_count = 10

    # don't instantiate through PKCS11SigningContext
    # also, just sign raw strings, we want to exercise the correctness of
    # the awaiting logic in sign_raw for object loading
    with _simple_sess() as sess:
        signer = pkcs11.PKCS11Signer(
            sess, SIGNER_LABEL, other_certs_to_pull=default_other_certs,
            bulk_fetch=bulk_fetch
        )

        async def _job(_i):
            payload = f"PKCS#11 concurrency test #{_i}!".encode('utf8')
            sig_result = await signer.async_sign_raw(payload, 'sha256')
            await asyncio.sleep(2)
            return _i, sig_result

        jobs = asyncio.as_completed(map(_job, range(1, concurrent_count + 1)))
        for finished_job in jobs:
            i, sig = await finished_job
            general.validate_raw(
                signature=sig,
                signed_data=f"PKCS#11 concurrency test #{i}!".encode('utf8'),
                cert=signer.signing_cert,
                md_algorithm='sha256',
                signature_algorithm=SignedDigestAlgorithm(
                    {'algorithm': 'sha256_rsa'}
                )
            )


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
def test_token_does_not_exist():

    with pytest.raises(PKCS11Error, match='No token with label.*found'):
        _simple_sess(token='aintnosuchtoken')


@pytest.mark.skipif(SKIP_PKCS11, reason="no PKCS#11 module")
def test_token_unclear():

    with pytest.raises(PKCS11Error, match='more than 1'):
        return pkcs11.open_pkcs11_session(
            pkcs11_test_module, user_pin='1234', token_label=None
        )


DUMMY_VER = {'major': 0, 'minor': 0}
DUMMY_ARGS = dict(
    serialNumber=b'\xde\xad\xbe\xef',
    slotDescription=b'', manufacturerID=b'',
    hardwareVersion=DUMMY_VER, firmwareVersion=DUMMY_VER,
)


class DummyToken(p11_types.Token):

    def open(self, rw=False, user_pin=None, so_pin=None):
        raise NotImplementedError


class DummySlot(p11_types.Slot):
    def __init__(self, lbl: Optional[str]):
        self.lbl = lbl

        super().__init__(
            "dummy.so.0", slot_id=0xdeadbeef,
            flags=(
                p11_types.SlotFlag(0) if lbl is None else
                p11_types.SlotFlag.TOKEN_PRESENT
            ),
            **DUMMY_ARGS,
        )

    def get_token(self):
        if self.lbl is not None:
            return DummyToken(
                self, label=self.lbl.encode('utf8'),
                model=b'DummyToken',
                flags=p11_types.TokenFlag(0),
                **DUMMY_ARGS
            )
        else:
            raise PKCS11Error("No token in slot")

    def get_mechanisms(self):
        return []

    def get_mechanism_info(self, mechanism):
        raise NotImplementedError


@pytest.mark.parametrize('slot_list,slot_no_query,token_lbl_query', [
    (('foo',), None, None),
    (('foo', 'bar'), 0, 'foo'),
    (('foo', None, 'bar'), 0, 'foo'),
    (('foo', None, 'bar'), None, 'foo'),
    # skip over empty slots when doing this scan
    ((None, 'foo', None, 'bar'), None, 'foo'),
    ((None, 'foo', None), 1, None),
])
def test_find_token(slot_list, slot_no_query, token_lbl_query):
    tok = find_token(
        [DummySlot(lbl) for lbl in slot_list],
        slot_no=slot_no_query, token_label=token_lbl_query
    )
    assert tok is not None
    if token_lbl_query:
        assert tok.label == token_lbl_query


@pytest.mark.parametrize('slot_list,slot_no_query,token_lbl_query, err', [
    (('foo', 'bar'), 2, 'foo', 'too large'),
    (('foo', 'bar'), 1, 'foo', 'Token in slot 1 is not \'foo\''),
    # when querying by slot, we want the error to be passed on
    ((None, 'bar'), 0, None, 'No token in'),
    (('foo', 'bar'), None, None, 'more than 1'),
    # right now, we don't care about the status of the slot in any way
    (('foo', None), None, None, 'more than 1'),
])
def test_find_token_error(slot_list, slot_no_query, token_lbl_query, err):
    with pytest.raises(PKCS11Error, match=err):
        find_token(
            [DummySlot(lbl) for lbl in slot_list],
            slot_no=slot_no_query, token_label=token_lbl_query
        )


@pytest.mark.parametrize('slot_list,token_lbl_query', [
    ((None, 'bar'), 'foo'),
    (('foo', 'bar'), 'baz'),
    ((None, None), 'foo'),
    ((), 'foo'),
])
def test_token_not_found(slot_list, token_lbl_query):
    tok = find_token(
        [DummySlot(lbl) for lbl in slot_list],
        slot_no=None, token_label=token_lbl_query
    )
    assert tok is None
