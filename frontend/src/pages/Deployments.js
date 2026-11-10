import React, { useState, useEffect } from 'react';
import { apiService } from '../services/api';
import { formatIST } from '../utils/timezone';
import '../styles/Deployments.css';

const statusBadgeClass = (status) => {
  switch (status) {
    case 'success': return 'status-badge status-success';
    case 'failed': return 'status-badge status-failed';
    case 'in_progress': return 'status-badge status-progress';
    default: return 'status-badge';
  }
};

const formatCommit = (commit) => (commit ? commit.substring(0, 7) : '—');

const formatTag = (tag) => {
  if (!tag) return '—';
  // "autopod-myapp:a82f91c-20260827-104233" -> show just the "version" part
  const parts = tag.split(':');
  return parts.length > 1 ? parts[1] : tag;
};

const Deployments = () => {
  const [deployments, setDeployments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchDeployments();
    const interval = setInterval(fetchDeployments, 8000);
    return () => clearInterval(interval);
  }, []);

  const fetchDeployments = async () => {
    try {
      const response = await apiService.getDeployments();
      if (response.data.success) {
        setDeployments(response.data.data || []);
        setError(null);
      }
    } catch (err) {
      console.error('Error fetching deployments:', err);
      setError(err.message || 'Failed to load deployment history');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="deployments-page">
      <div className="page-header">
        <h1>🚀 Deployment History</h1>
        <p>Every build/deploy attempt triggered by a GitHub push, a manual deploy, or a rollback.</p>
      </div>

      <div className="deployments-actions">
        <button className="btn btn-secondary" onClick={fetchDeployments}>
          <span className="btn-icon">🔄</span>
          <span className="btn-text">Refresh</span>
        </button>
      </div>

      {loading ? (
        <div className="loading">Loading deployment history...</div>
      ) : error ? (
        <div className="empty-state error">
          <div className="empty-icon">⚠️</div>
          <h3>Couldn't load deployments</h3>
          <p>{error}</p>
        </div>
      ) : deployments.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">🚀</div>
          <h3>No Deployments Yet</h3>
          <p>Push to a connected GitHub repository, or use the manual deploy form on the Dashboard, to see history here.</p>
        </div>
      ) : (
        <div className="deployments-table-wrapper">
          <table className="deployments-table">
            <thead>
              <tr>
                <th>Version</th>
                <th>Commit</th>
                <th>Target</th>
                <th>Status</th>
                <th>Trigger</th>
                <th>Started</th>
                <th>Finished</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map((d) => (
                <tr key={d.id} className={d.is_rollback ? 'row-rollback' : ''}>
                  <td className="cell-version">
                    {formatTag(d.image_tag)}
                    {d.is_rollback ? <span className="rollback-tag">rollback</span> : null}
                  </td>
                  <td className="cell-mono">{formatCommit(d.git_commit)}</td>
                  <td className="cell-target">
                    {d.target_node ? (
                      <span className="target-node-pill" title={`Worker node: ${d.target_node}`}>
                        🖥️ {d.target_node}
                      </span>
                    ) : (
                      <span className="target-orchestrator-pill" title="Overall pipeline build & rollout orchestrator">
                        ⚙️ Pipeline
                      </span>
                    )}
                  </td>
                  <td><span className={statusBadgeClass(d.status)}>{d.status}</span></td>
                  <td>{d.triggered_by || '—'}</td>
                  <td className="cell-time">{formatIST(d.started_at)}</td>
                  <td className="cell-time">{formatIST(d.finished_at)}</td>
                  <td className="cell-message" title={d.message || ''}>{d.message || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default Deployments;
