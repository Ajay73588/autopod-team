import React, { useState, useEffect, useCallback } from 'react';
import { apiService } from '../services/api';
import '../styles/Fleet.css';

const summarizeRollout = (d) => {
  const results = (d.data && d.data.results) || [];
  const ok = results.filter((r) => r.status === 'success').length;
  const status = d.data && d.data.status;
  return `Rollout ${status || 'finished'}: ${ok}/${results.length} node(s) succeeded.` +
    (status === 'partial_failure' ? ' Stopped at first failure; remaining nodes were left on their previous version.' : '');
};

const summarizeSync = (d) => 'Nodes synced from the Auto Scaling Group.' + (d.data ? ` ${JSON.stringify(d.data)}` : '');

const formatCommit = (commit) => (commit ? commit.substring(0, 7) : '—');

const emptyForm = { name: '', repo_url: '', target_port: '', health_check_path: '/' };

const Fleet = () => {
  const [apps, setApps] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [versions, setVersions] = useState([]);
  const [selectedApp, setSelectedApp] = useState('');
  const [form, setForm] = useState(emptyForm);
  const [asgName, setAsgName] = useState('');
  const [message, setMessage] = useState(null); // {ok, text}
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null); // {kind, versionId}

  const refresh = useCallback(async () => {
    try {
      const [a, n] = await Promise.all([apiService.getApplications(), apiService.getAppNodes()]);
      setApps(a.data.data || []);
      setNodes(n.data.data || []);
    } catch (err) {
      setMessage({ ok: false, text: err.message });
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (!selectedApp) { setVersions([]); return; }
    apiService.getVersionsFor(selectedApp)
      .then((r) => setVersions(r.data.data || []))
      .catch((err) => setMessage({ ok: false, text: err.message }));
  }, [selectedApp]);

  const run = async (fn, okText) => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await fn();
      const text = typeof okText === 'function' ? okText(r.data) : okText;
      // 207 (partial failure) is a 2xx response, so check the success flag.
      setMessage({ ok: r.data.success !== false, text });
      await refresh();
    } catch (err) {
      setMessage({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  };

  const register = (e) => {
    e.preventDefault();
    const payload = { name: form.name.trim(), repo_url: form.repo_url.trim(), health_check_path: form.health_check_path || '/' };
    if (form.target_port) payload.target_port = Number(form.target_port);
    run(() => apiService.registerApplication(payload), `Registered ${payload.name}.`)
      .then(() => setForm(emptyForm));
  };

  const confirmRollout = () => {
    const { kind, versionId } = pending;
    setPending(null);
    const call = kind === 'rollback' ? apiService.rollingRollback : apiService.rollingDeploy;
    run(() => call(Number(selectedApp), versionId), summarizeRollout);
  };

  return (
    <div className="fleet-page">
      <h1>Fleet</h1>
      {message && <div className={`fleet-msg ${message.ok ? 'ok' : 'err'}`}>{message.text}</div>}

      <section className="fleet-card">
        <h2>Applications</h2>
        <table>
          <thead><tr><th>Name</th><th>Repo</th><th>Port</th><th>Health path</th></tr></thead>
          <tbody>
            {apps.length === 0 && <tr><td colSpan="4">No applications registered.</td></tr>}
            {apps.map((a) => (
              <tr key={a.id}><td>{a.name}</td><td>{a.repo_url || '—'}</td><td>{a.target_port || '—'}</td><td>{a.health_check_path}</td></tr>
            ))}
          </tbody>
        </table>
        <form onSubmit={register} className="fleet-form">
          <input placeholder="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          <input placeholder="repo URL" value={form.repo_url} onChange={(e) => setForm({ ...form, repo_url: e.target.value })} />
          <input placeholder="port" type="number" min="1" max="65535" value={form.target_port} onChange={(e) => setForm({ ...form, target_port: e.target.value })} />
          <input placeholder="health path" value={form.health_check_path} onChange={(e) => setForm({ ...form, health_check_path: e.target.value })} />
          <button type="submit" disabled={busy}>Register</button>
        </form>
      </section>

      <section className="fleet-card">
        <h2>App nodes</h2>
        <table>
          <thead><tr><th>IP</th><th>Status</th><th>Last seen</th></tr></thead>
          <tbody>
            {nodes.length === 0 && <tr><td colSpan="3">No nodes registered. Sync from the Auto Scaling Group once AWS is set up.</td></tr>}
            {nodes.map((n) => (
              <tr key={n.id}><td>{n.ip_address}</td><td>{n.status}</td><td>{n.last_seen_at || '—'}</td></tr>
            ))}
          </tbody>
        </table>
        <div className="fleet-form">
          <input placeholder="ASG name (optional, defaults to server config)" value={asgName} onChange={(e) => setAsgName(e.target.value)} />
          <button disabled={busy} onClick={() => run(() => apiService.syncAppNodes(asgName.trim()), summarizeSync)}>Sync from ASG</button>
        </div>
      </section>

      <section className="fleet-card">
        <h2>Rolling rollout</h2>
        <select value={selectedApp} onChange={(e) => setSelectedApp(e.target.value)}>
          <option value="">Select application…</option>
          {apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        {selectedApp && (
          <table>
            <thead><tr><th>Image</th><th>Commit</th><th /></tr></thead>
            <tbody>
              {versions.length === 0 && <tr><td colSpan="3">No versions recorded.</td></tr>}
              {versions.map((v) => (
                <tr key={v.id}>
                  <td>{v.image_tag}{v.is_current ? ' (current)' : ''}</td>
                  <td>{formatCommit(v.git_commit || v.commit_sha)}</td>
                  <td>
                    <button disabled={busy || nodes.length === 0} onClick={() => setPending({ kind: 'deploy', versionId: v.id })}>Roll out</button>{' '}
                    <button disabled={busy || nodes.length === 0} onClick={() => setPending({ kind: 'rollback', versionId: v.id })}>Roll back to</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {pending && (
          <div className="fleet-confirm">
            <p>
              {pending.kind === 'rollback' ? 'Roll back' : 'Roll out'} version #{pending.versionId} to {nodes.filter((n) => n.status === 'in_service').length} in-service node(s), one at a time. It stops at the first failure.
            </p>
            <button onClick={confirmRollout}>Confirm</button>{' '}
            <button onClick={() => setPending(null)}>Cancel</button>
          </div>
        )}
      </section>
    </div>
  );
};

export default Fleet;
