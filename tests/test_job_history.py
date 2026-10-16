"""Private GUI history, artifact cleanup, and retained replay dependencies."""

import os

import pytest

from test_core_app import app, gui
from test_world_jobs import OWNER_B, submission
from worldprompt_core.world_jobs import JobSupervisor


def test_history_includes_prompt_dimensions_and_saved_settings(app):
    request = submission(prompt='Build a <woodland> cabin', effort='high')
    job = app.supervisor.submit(request)
    current = app.snapshot()['jobs'][0]
    assert current['prompt'] == request['prompt']
    assert current['dimensions'] == [3, 2, 2]
    assert current['effort'] == 'high'
    assert current['model'] == (app.config['codex_model'] or 'Codex default')
    app.supervisor.cancel(job['id'])
    snapshot = app.snapshot()
    assert snapshot['recent_jobs'][0]['prompt'] == request['prompt']
    assert snapshot['recent_jobs'][0]['cleanup_blocked_reason'] is None
    assert snapshot['cleanup_job_ids'] == [job['id']]
    assert snapshot['recent_job_count'] == 1


def test_cleanup_is_authenticated_and_rejects_active_jobs_atomically(app, gui):
    finished = app.supervisor.submit(submission())
    app.supervisor.cancel(finished['id'])
    active = app.supervisor.submit(submission(OWNER_B))
    body = {'job_ids': [finished['id']]}
    for headers in ({'X-WorldPrompt-Session': 'wrong'}, {'Origin': 'https://example.com'}):
        assert gui('POST', '/api/jobs/remove', body, headers)[0] == 403
    assert gui('POST', '/api/jobs/remove', {'job_ids': [finished['id'], active['id']]})[0] == 400
    assert (app.supervisor.directory / finished['id'] / 'request.json').is_file()
    status, _, result = gui('POST', '/api/jobs/remove', body)
    assert status == 200 and result['removed'] == 1
    assert result['state']['recent_job_count'] == 0
    assert result['state']['jobs'][0]['id'] == active['id']
    assert not (app.supervisor.directory / finished['id']).exists()
    assert (app.supervisor.directory / 'supervisor.lock').exists()
    assert app.token_path.exists()
    app.supervisor.close()
    recovered = JobSupervisor(app.config, start=False)
    try:
        assert finished['id'] not in recovered.jobs
        assert active['id'] in recovered.jobs
    finally:
        recovered.close()


@pytest.mark.parametrize('ids', [None, [], 'all', [None], ['../outside'], ['no-such-job']])
def test_cleanup_rejects_invalid_ids(app, gui, ids):
    assert gui('POST', '/api/jobs/remove', {'job_ids': ids})[0] == 400


def test_bulk_cleanup_includes_history_beyond_visible_eight(app, gui):
    for index in range(10):
        job = app.supervisor.submit(submission(prompt=f'Build {index}'))
        app.supervisor.cancel(job['id'])
    snapshot = app.snapshot()
    assert len(snapshot['recent_jobs']) == 8
    assert snapshot['recent_job_count'] == 10
    status, _, result = gui('POST', '/api/jobs/remove', {'job_ids': snapshot['cleanup_job_ids']})
    assert status == 200 and result['removed'] == 10
    assert app.supervisor.jobs == {}
    assert not list(app.supervisor.directory.glob('*/job.json'))
    assert not list(app.supervisor.directory.glob('*/request.json'))


def test_cleanup_storage_failure_keeps_every_job_listed_and_intact_for_retry(app, monkeypatch):
    first = app.supervisor.submit(submission())
    second = app.supervisor.submit(submission(OWNER_B))
    for job in (first, second):
        app.supervisor.cancel(job['id'])
    real_replace = os.replace
    moves = []
    def fail_second(source, destination):
        moves.append(source)
        if len(moves) == 2:
            raise OSError('Fixture permission denied')
        return real_replace(source, destination)
    monkeypatch.setattr('worldprompt_core.world_jobs.os.replace', fail_second)
    with pytest.raises(OSError):
        app.supervisor.remove_jobs([first['id'], second['id']])
    monkeypatch.undo()
    for job in (first, second):
        assert job['id'] in app.supervisor.jobs
        assert (app.supervisor.directory / job['id'] / 'job.json').is_file()
    assert not list(app.supervisor.directory.glob('.trash-*'))
    assert app.supervisor.remove_jobs([first['id'], second['id']]) == 2
