import React, { useState } from 'react';
import { ExternalLink, Copy, Check, RefreshCw, X, CheckCircle, AlertCircle, Shield } from 'lucide-react';
import { apiService } from '../services/api';
import '../styles/DeploymentResultCard.css';

const DeploymentResultCard = ({ result, onDismiss }) => {
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [verifyingWebhook, setVerifyingWebhook] = useState(false);
  const [webhookDeliveries, setWebhookDeliveries] = useState(null);

  if (!result) return null;

  // Canonical HTTPS Webhook URL (always HTTPS, canonical host 3-239-41-33.sslip.io or current live HTTPS origin)
  const canonicalWebhookUrl = (typeof window !== 'undefined' && window.location?.hostname && !['localhost', '127.0.0.1'].includes(window.location.hostname))
    ? `${window.location.protocol === 'https:' ? window.location.origin : 'https://' + window.location.host}/webhook`
    : 'https://3-239-41-33.sslip.io/webhook';

  const handleCopyWebhookUrl = () => {
    try {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(canonicalWebhookUrl);
        setCopiedUrl(true);
        setTimeout(() => setCopiedUrl(false), 2500);
      }
    } catch (err) {
      console.warn('Clipboard write failed:', err);
    }
  };

  const handleVerifyWebhook = async () => {
    try {
      setVerifyingWebhook(true);
      const res = await apiService.getWebhookDeliveries(5);
      if (res?.data?.data) {
        setWebhookDeliveries(res.data.data);
      } else {
        setWebhookDeliveries([]);
      }
    } catch (err) {
      console.error('Error fetching webhook deliveries:', err);
      setWebhookDeliveries([]);
    } finally {
      setVerifyingWebhook(false);
    }
  };

  const isSuccess = result.status === 'success' || !result.status;
  const appName = result.repo_name || result.name || result.app_name || 'Application';
  const containerName = result.container_name || `${appName}-container`;
  const port = result.port || 'Auto';
  const publicUrl = result.public_url || null;
  const deploymentId = result.deployment_id || result.id || 'N/A';

  return (
    <div className={`deployment-result-card ${isSuccess ? 'card-success' : 'card-error'}`}>
      {/* Header */}
      <div className="drc-header">
        <div className="drc-header-title">
          {isSuccess ? (
            <span className="drc-badge success-badge">
              <CheckCircle size={14} /> Deployed Successfully
            </span>
          ) : (
            <span className="drc-badge error-badge">
              <AlertCircle size={14} /> Deployment Failed
            </span>
          )}
          <h3 className="drc-app-title">{appName}</h3>
          <span className="drc-id-pill">ID #{deploymentId}</span>
        </div>
        {onDismiss && (
          <button 
            type="button" 
            className="drc-btn-close" 
            onClick={onDismiss} 
            title="Dismiss deployment details"
            aria-label="Dismiss deployment card"
          >
            <X size={18} />
          </button>
        )}
      </div>

      {/* Overview Metadata Grid */}
      <div className="drc-meta-grid">
        <div className="drc-meta-item">
          <span className="drc-meta-label">Application:</span>
          <span className="drc-meta-value">{appName}</span>
        </div>
        <div className="drc-meta-item">
          <span className="drc-meta-label">Container:</span>
          <span className="drc-meta-value code-font">{containerName}</span>
        </div>
        <div className="drc-meta-item">
          <span className="drc-meta-label">Worker Port:</span>
          <span className="drc-meta-value code-font">{port}</span>
        </div>
        <div className="drc-meta-item drc-meta-url">
          <span className="drc-meta-label">Public Route:</span>
          <span className="drc-meta-value">
            {publicUrl ? (
              <a href={publicUrl} target="_blank" rel="noopener noreferrer" className="drc-live-link">
                {publicUrl}
              </a>
            ) : (
              <span className="drc-text-muted">Route active on worker port {port}</span>
            )}
          </span>
        </div>
      </div>

      {/* Primary Action Button */}
      {publicUrl && (
        <div className="drc-primary-action-row">
          <a
            href={publicUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="btn btn-primary drc-open-btn"
          >
            <ExternalLink size={16} />
            <span>Open Application</span>
          </a>
        </div>
      )}

      {/* GitHub Push-to-Deploy Continuous Deployment Setup */}
      <div className="drc-instructions-panel">
        <div className="drc-panel-heading">
          <Shield size={16} className="drc-heading-icon" />
          <h4>GitHub Continuous Deployment (Push-to-Deploy)</h4>
        </div>
        <p className="drc-panel-subtext">
          Configure an automated GitHub webhook so every commit pushed to this repository automatically triggers a build, container rollout, and health check:
        </p>

        <ol className="drc-steps-list">
          <li>
            Open your repository on GitHub and navigate to:
            <span className="drc-path-highlight"> Settings → Webhooks → Add webhook</span>
          </li>
          <li>
            <div className="drc-field-group">
              <label className="drc-field-label">Payload URL (Canonical HTTPS):</label>
              <div className="drc-copy-box">
                <input
                  type="text"
                  readOnly
                  value={canonicalWebhookUrl}
                  className="drc-input-url"
                  onClick={(e) => e.target.select()}
                />
                <button
                  type="button"
                  className={`btn drc-btn-copy ${copiedUrl ? 'copied' : ''}`}
                  onClick={handleCopyWebhookUrl}
                  title="Copy Canonical Webhook URL to clipboard"
                >
                  {copiedUrl ? <Check size={14} /> : <Copy size={14} />}
                  <span>{copiedUrl ? 'Copied!' : 'Copy URL'}</span>
                </button>
              </div>
            </div>
          </li>
          <li>
            <strong>Content type:</strong> Select <code>application/json</code>
          </li>
          <li>
            <strong>Secret:</strong> Enter your configured AutoPod Webhook Secret (<code>GITHUB_WEBHOOK_SECRET</code>).
          </li>
          <li>
            <strong>Which events would you like to trigger this webhook?:</strong> Select <strong>Just the push event</strong>
          </li>
          <li>
            Ensure <strong>Active</strong> is checked, then click <strong>Add webhook</strong>.
          </li>
        </ol>

        {/* Verification Trigger */}
        <div className="drc-verify-box">
          <div className="drc-verify-actions">
            <button
              type="button"
              className={`btn btn-secondary drc-verify-btn ${verifyingWebhook ? 'loading' : ''}`}
              onClick={handleVerifyWebhook}
              disabled={verifyingWebhook}
            >
              <RefreshCw size={14} className={verifyingWebhook ? 'spin' : ''} />
              <span>{verifyingWebhook ? 'Checking Deliveries...' : 'Check Webhook Delivery Status'}</span>
            </button>
            <span className="drc-verify-hint">
              Sends an inspection query to verify recent GitHub ping and push deliveries.
            </span>
          </div>

          {webhookDeliveries !== null && (
            <div className="drc-delivery-status-panel">
              {webhookDeliveries.length > 0 ? (
                <div className="drc-delivery-table-wrap">
                  <p className="drc-delivery-summary">
                    ✅ Webhook endpoint verified! Recorded deliveries:
                  </p>
                  <table className="drc-delivery-table">
                    <thead>
                      <tr>
                        <th>Event</th>
                        <th>Delivery ID</th>
                        <th>Received (IST)</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {webhookDeliveries.slice(0, 3).map((d) => (
                        <tr key={d.id || d.delivery_id}>
                          <td><span className="drc-event-tag">{d.event_type || 'push'}</span></td>
                          <td className="code-font">{d.delivery_id ? `${d.delivery_id.substring(0, 16)}...` : 'N/A'}</td>
                          <td>{d.received_at || 'Just now'}</td>
                          <td>
                            <span className="drc-status-pill success-pill">
                              {d.status || 'processed'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="drc-no-deliveries">
                  <span>⏳ No webhook deliveries recorded yet. Save the webhook in GitHub to send a test ping.</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default DeploymentResultCard;
