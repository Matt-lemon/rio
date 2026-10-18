"""Offline tests for CI license gate. No package downloads or network access."""
import json
from email.message import Message
from pathlib import Path
import subprocess
from types import SimpleNamespace

from tools.ci import license_guard as guard
from tools.ci.run_tests import command
import pytest


def policy():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root/'.github/license-policy.json').read_text())


def fake_dist(name, version, license_expression=None, license_field=None, classifiers=None):
    meta = Message()
    meta['Name'] = name
    if license_expression is not None:
        meta['License-Expression'] = license_expression
    if license_field is not None:
        meta['License'] = license_field
    for classifier in classifiers or []:
        meta['Classifier'] = classifier
    return SimpleNamespace(metadata=meta, version=version)


def test_valid_permissive_and_composite_spdx_licenses():
    allowed = set(policy()['allowed_licenses'])
    assert guard.license_ids('MIT OR Apache-2.0', allowed)=={'MIT','Apache-2.0'}
    assert guard.license_ids('MIT AND (Apache-2.0)',allowed)=={'MIT','Apache-2.0'}
    assert guard.license_ids('MIT-0',allowed)=={'MIT-0'}
    assert guard.license_ids('MIT WITH Classpath-exception-2.0',allowed) is None


def test_copyleft_not_accepted_implicitly():
    p=policy()
    data=[fake_dist('bad', '1.0', 'GPL-3.0-only OR MIT'),
          fake_dist('unknown', '3.0', None, 'Mystery EULA')]
    errors, inventory=guard.guard_dependencies(p,data)
    assert len(errors)==2
    assert all(x.startswith('UNAPPROVED_LICENSE:') for x in errors)
    assert len(inventory)==2


def test_ambiguous_classifier_denied():
    allowed=set(policy()['allowed_licenses'])
    d=fake_dist('ambiguous', '1', classifiers=['License :: OSI Approved :: BSD License'])
    assert guard.distribution_licenses(d,allowed) is None


def test_known_metadata_classifier_is_allowed():
    allowed=set(policy()['allowed_licenses'])
    d=fake_dist('good', '1', classifiers=['License :: OSI Approved :: MIT License'])
    assert guard.distribution_licenses(d,allowed)=={'MIT'}


def test_review_exceptions_are_version_and_reason_scoped():
    p=policy()
    p['reviewed_exceptions']['review-me']={
        'version':'2.1.0', 'license':'MIT', 'reason':'Audited explicit upstream text and notices'
    }
    errors,_=guard.guard_dependencies(p,[fake_dist('review_me','2.1.0',license_field='unknown')])
    assert errors==[]
    errors,_=guard.guard_dependencies(p,[fake_dist('review_me','2.2.0',license_field='unknown')])
    assert len(errors)==1


def test_binary_bundles_require_explicit_source_license_notice(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    (tmp_path/'weights.gguf').write_bytes(b'GGUF\x01')
    subprocess.run(['git','-C',str(tmp_path),'add','weights.gguf'],check=True)
    p=policy()
    p['pending_human_review_assets']=[]
    errors,_=guard.guard_artifacts(tmp_path,p)
    assert errors==['UNREVIEWED_BINARY: weights.gguf']
    (tmp_path/'NOTICE').write_text('Reviewed upstream notice\n')
    p['reviewed_artifacts']['weights.gguf']={
        'license':'MIT','source':'https://example.com/source','notice':'NOTICE'
    }
    errors,approved=guard.guard_artifacts(tmp_path,p)
    assert errors==[] and approved==['weights.gguf']


def test_missing_gateway_fails_closed_without_deselection(tmp_path):
    with pytest.raises(FileNotFoundError, match='REVIEWED_GATEWAY_SOURCE_REQUIRED_FOR_CI'):
        command(tmp_path)
    (tmp_path/'integrations/google_home/nas').mkdir(parents=True)
    (tmp_path/'integrations/google_home/nas/gateway.py').write_text('')
    cmd=command(tmp_path)
    assert '--deselect' not in cmd


def test_new_unreviewed_image_is_blocked(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    (tmp_path/'picture.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    subprocess.run(['git','-C',str(tmp_path),'add','picture.png'],check=True)
    p=policy()
    p['pending_human_review_assets']=[]
    errors,_=guard.guard_artifacts(tmp_path,p)
    assert errors==['UNREVIEWED_MEDIA: picture.png']
