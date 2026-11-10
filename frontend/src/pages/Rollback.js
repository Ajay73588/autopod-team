import React, { useState, useEffect } from 'react';
import { apiService } from '../services/api';
import { formatIST } from '../utils/timezone';
import '../styles/Rollback.css';

const formatTag = (tag) => {
  if (!tag) return '—';
  const parts = tag.split(':');
  return parts.length > 1 ? parts[1] : tag;
};

const Rollback = () => {
  const [applications, setApplications] = useState([]);
  const [selectedAppId, setSelectedAppId] = useState(null);
  const [versions, setVersions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [pendingVersion, setPendingVersion] = useState(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [result, setResult] = useState(null);

  // Load applications on mount
  useEffect(() => {
    fetchApplications();
  }, []);

  // When selected application changes, load its versions
  useEffect(() => {
    if (selectedAppId) {
      fetchVersions(selectedAppId);
    }
  }, [selectedAppId]);

  const fetchApplications = async () => {
    try {
      setLoading(true);
      const resp = await apiService.getApplications();
      const apps = resp.data?.data || resp.data || [];
      setApplications(apps);
      if (apps.length > 0) {
        setSelectedAppId(apps[0].id);
      } else {
        setLoading(false);
      }
    } catch (err) {
      console.error('Error fetching applications:', err);
      setError(err.message || 'Failed to load applications');
      setLoading(false);
    }
  };

  const fetchVersions = async (appId) => {
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.getVersions(appId);
      if (response.data.success) {
        setVersions(response.data.data || []);
      }
    } catch (err) {
      console.error('Error fetching versions:', err);
      setError(err.message || 'Failed to load versions');
    } finally {
      setLoading(false);
    }
  };

  const selectedApp = applications.find((a) => a.id === selectedAppId);
  const currentVersion = versions.find((v) => v.is_current);

  const requestRollback = (version) => {
    setResult(null);
    setPendingVersion(version);
  };

  const cancelRollback = () => setPendingVersion(null);

  const confirmRollback = async () => {
    if (!pendingVersion || !selectedAppId) return;
    setRollingBack(true);
    setResult(null);
    try {
      const response = await apiService.rollbackToVersion(pendingVersion.id, selectedAppId);
      const deployment = response.data.deployment;
      if (response.data.success) {
        setResult({
          ok: true,
          message: `Rolled back to ${formatTag(pendingVersion.image_tag)} successfully.`,
        });
      } else {
        setResult({
          ok: false,
          message: deployment?.message || response.data?.message || 'Rollback failed.',
        });
      }
    } catch (err) {
      setResult({ ok: false, message: err.message || 'Rollback failed.' });
    } finally {
      setRollingBack(false);
      setPendingVersion(null);
      fetchVersions(selectedAppId);
    }
  };

  return (
    <div className="rollback-page">
      <div className="page-header">
        <h1>⏪ Rollback</h1>
        <p>
          Redeploy a previously built version. The current version keeps running until the rollback is confirmed healthy.
        </p>
      </div>

      {/* Application Selector */}
      <div className="app-selector-card" style={{
        marginBottom: '1.25rem',
        padding: '1rem 1.25rem',
        background: '#fff',
        border: '1px solid #e2e8f0',
        borderRadius: '8px',
        display: 'flex',
        alignItems: 'center',
        gap: '1rem',
        flexWrap: 'wrap'
      }}>
        <label htmlFor="rollback-app-select" style={{ fontWeight: 600, color: '#334155' }}>
          Select Application:
        </label>
        <select
          id="rollback-app-select"
          value={selectedAppId || ''}
          onChange={(e) => setSelectedAppId(Number(e.target.value))}
          style={{
            padding: '0.5rem 0.85rem',
            borderRadius: '6px',
            border: '1px solid #cbd5e1',
            fontSize: '0.95rem',
            minWidth: '240px',
            background: '#fff'
          }}
        >
          {applications.map((app) => (
            <option key={app.id} value={app.id}>
              {app.name} ({app.routing_path || '/'})
            </option>
          ))}
        </select>

        {selectedApp && (
          <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.85rem', color: '#64748b' }}>
            <span>Target Port: <strong>{selectedApp.target_port || '—'}</strong></span>
            <span>•</span>
            <span>Path: <strong>{selectedApp.routing_path || '/'}</strong></span>
            {selectedApp.container_name && (
              <>
                <span>•</span>
                <span>Container: <strong>{selectedApp.container_name}</strong></span>
              </>
            )}
          </div>
        )}
      </div>

      {currentVersion && (
        <div className="current-version-banner">
          Current version: <strong>{formatTag(currentVersion.image_tag)}</strong>
          {currentVersion.git_commit && (
            <span style={{ marginLeft: '0.5rem', opacity: 0.85 }}>
              ({currentVersion.git_commit.substring(0, 7)})
            </span>
          )}
        </div>
      )}

      {result && (
        <div className={`rollback-result ${result.ok ? 'rollback-result-ok' : 'rollback-result-error'}`}>
          {result.ok ? '✅' : '❌'} {result.message}
        </div>
      )}

      {loading ? (
        <div className="loading" style={{ padding: '2rem', textAlign: 'center', color: '#64748b' }}>
          Loading versions for {selectedApp?.name || 'application'}...
        </div>
      ) : error ? (
        <div className="empty-state error">
          <div className="empty-icon">⚠️</div>
          <h3>Couldn't load versions</h3>
          <p>{error}</p>
        </div>
      ) : versions.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">⏪</div>
          <h3>No Versions Available</h3>
          <p>Deploy "{selectedApp?.name}" at least once to see historical versions here.</p>
        </div>
      ) : (
        <div className="versions-list">
          {versions.map((v) => (
            <div key={v.id} className={`version-row ${v.is_current ? 'version-row-current' : ''}`}>
              <div className="version-info">
                <span className="version-tag">{formatTag(v.image_tag)}</span>
                {v.git_commit && <span className="version-commit">{v.git_commit.substring(0, 7)}</span>}
                <span className="version-date">{formatIST(v.created_at)}</span>
              </div>
              {v.is_current ? (
                <span className="current-pill">CURRENT</span>
              ) : (
                <button
                  className="btn btn-warning btn-sm"
                  onClick={() => requestRollback(v)}
                  disabled={rollingBack}
                >
                  Rollback
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {pendingVersion && (
        <div className="rollback-confirm-overlay">
          <div className="rollback-confirm-dialog">
            <h3>Confirm rollback</h3>
            <p>
              This will deploy <strong>{formatTag(pendingVersion.image_tag)}</strong> for{' '}
              <strong>{selectedApp?.name}</strong> and health-check it before replacing the current running version.
              If it fails to start or become healthy, the current version is left running untouched.
            </p>
            <div className="rollback-confirm-actions">
              <button className="btn btn-secondary" onClick={cancelRollback} disabled={rollingBack}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={confirmRollback} disabled={rollingBack}>
                {rollingBack ? 'Rolling back…' : 'Confirm Rollback'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Rollback;
