import json
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from ingestion import catalogue as cat
from ingestion.catalogue_encoder import CatalogueEncoder, DIMENSION, VECTOR_NAME
from ingestion.catalogue_meta import listing_reports, enrich_report, document_metadata, canonical_pdf
from ingestion.crawler import START_URL, next_listing


CARD = '''<div class="AuditReportlisting"><div class="dateFirst"><span class="dtn">02 September 2026</span>
<div class="reportType"><span>Financial</span></div></div>
<div class="reportIcon"><img src="/uploads/states/upload/himachal-pradesh.png"><h5>हिमाचल प्रदेश</h5></div>
<a href="/hi/audit-report/details/123">राज्य के वित्त पर प्रतिवेदन 2024-25</a>
<div class="sectorDetail"><div>Sector:</div><div>Finance</div></div>
<div class="pdfBottomReport"><a href="/webroot/uploads/download_audit_report/test-Hindi.pdf">Full report</a>
<a href="/webroot/uploads/download_audit_report/test-English.pdf">Full report English</a></div></div>'''


def test_official_and_derived_metadata():
    report = listing_reports(CARD, 'https://cag.gov.in/hi/audit-report')[0]
    assert report['jurisdiction'] == 'himachal-pradesh'
    assert report['official_report_types'] == ['Financial']
    assert len(report['pdfs']) == 2
    detail = '<div class="auditRightLable"><div class="labelTitleBold">Government Type</div><div class="labelItemBold">State</div></div>'
    report = enrich_report(report, detail)
    doc = document_metadata(report, report['pdfs'][0])
    assert doc['categories'] == ['financial', 'state-finances']
    assert doc['language'] == 'hi'
    assert doc['government_type'] == 'state'
    assert doc['published_year'] == 2026
    assert doc['audit_periods'] == ['2024-25']
    assert doc['source'] == 'cag'


def test_canonical_alias_and_external_rejection():
    assert canonical_pdf(START_URL, '/webroot/uploads/download_audit_report/a.pdf') == canonical_pdf(START_URL, '/uploads/download_audit_report/a.pdf')
    with pytest.raises(ValueError):
        canonical_pdf(START_URL, 'https://example.com/download_audit_report/a.pdf')


def test_chapter_fallback_and_no_navigation_pdfs():
    report = listing_reports(CARD, START_URL)[0]
    report['pdfs'] = []
    html = '<a href="/uploads/download_audit_report/nav.pdf">Nav</a><ul class="guidelinesList"><li><a href="/uploads/download_audit_report/ch1.pdf">Chapter I</a></li></ul>'
    result = enrich_report(report, html)
    assert result['coverage_strategy'] == 'section-fallback'
    assert len(result['pdfs']) == 1
    with pytest.raises(ValueError):
        enrich_report(report, '')


def test_hindi_pagination():
    base = 'https://cag.gov.in/hi/audit-report'
    assert next_listing('<a href="?page=2">Next</a>', base) == base + '?page=2'


class Site:
    def __init__(self, html): self.html = html
    def get(self, url): return self.html
    def small_text(self, value): return value


def test_discovery_checkpoint_dedup_and_two_locales(tmp_path):
    with closing(cat.connect(tmp_path)) as db:
        cat.discover(db, Site(CARD))
        assert cat.state(db, 'next_listing') == 'https://cag.gov.in/hi/audit-report'
        cat.discover(db, Site(CARD))
        assert cat.state(db, 'next_listing') is None
        assert cat.state(db, 'catalogue_completed_at') > 0
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 2
        assert not cat.discover(db, Site(CARD))


def test_bad_pagination_does_not_advance_checkpoint(tmp_path):
    with closing(cat.connect(tmp_path)) as db:
        with pytest.raises(ValueError, match='Pagination'):
            cat.discover(db, Site(CARD + '<a href="?page=300">Last</a>'))
        assert cat.state(db, 'next_listing', START_URL) == START_URL
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 0


def test_failure_backoff_and_exhaustion(tmp_path):
    with closing(cat.connect(tmp_path)) as db:
        db.execute("INSERT INTO documents(id,metadata) VALUES ('x','{}')"); db.commit()
        for i in range(5):
            row = db.execute('SELECT * FROM documents').fetchone()
            cat.failed(db, 'documents', row, ValueError('bad PDF'))
        row = db.execute('SELECT * FROM documents').fetchone()
        assert row['status'] == 'failed' and row['attempts'] == 5
        assert cat.due(db, 'documents') is None


def test_profile_schema_and_filters():
    with closing(QdrantClient(':memory:')) as client:
        cat.ensure_collection(client, 'profile')
        for jurisdiction, ready in [('nagaland',True),('manipur',True),('nagaland',False)]:
            client.upsert(cat.COLLECTION, [models.PointStruct(id=str(uuid.uuid4()), vector={VECTOR_NAME:[1.0]*DIMENSION},
                payload={'embedding_profile':'profile', 'jurisdiction':jurisdiction, 'categories':['state-finances'],
                         'ready':ready, 'published_year':2026})])
        f = cat.query_filter('profile', jurisdiction='Nagaland', category='state finances', year=2026)
        assert client.count(cat.COLLECTION, count_filter=f, exact=True).count == 1
        with pytest.raises(ValueError, match='another model'):
            cat.ensure_collection(client, 'different')


def test_multilingual_window_splitting_preserves_all_text():
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    encoder = CatalogueEncoder.__new__(CatalogueEncoder)
    encoder.tokenizer = Tokenizer(WordLevel({'[UNK]':0}, unk_token='[UNK]'))
    encoder.tokenizer.pre_tokenizer = Whitespace()
    encoder.limit = 8
    original = 'भारत budget वित्त public debt. ' * 100
    windows = list(encoder.windows(original))
    assert ''.join(part for part, _ in windows) == original
    assert all(len(encoder.tokenizer.encode(part).ids) <= 8 for part, _ in windows)


def test_facets_only_include_indexed_documents(tmp_path):
    with closing(cat.connect(tmp_path)) as db:
        for id, status in [('a','indexed'),('b','pending')]:
            db.execute('INSERT INTO documents(id,metadata,status) VALUES (?,?,?)',
                       (id,json.dumps({'jurisdiction':'nagaland','categories':['financial'],'language':'en'}),status))
        db.commit()
        assert cat.facets(db)['jurisdiction'] == {'nagaland':1}


def test_disk_reserve_prevents_work(tmp_path, monkeypatch):
    from ingestion.catalogue_pdf import reserve
    monkeypatch.setenv('CAG_MIN_FREE_BYTES', str(10**30))
    with pytest.raises(OSError, match='disk space'):
        reserve(tmp_path)