import React from 'react';
import { Play, Square, RotateCcw, Trash2, Globe, Loader, Download } from 'lucide-react';
import { apiService } from '../services/api';

const ContainerActions = ({ container, onAction, loadingActions = {} }) => {
  const [isDownloading, setIsDownloading] = React.useState(false);
  const containerName = container.Names?.[0] || container.container_name;
  const status = container.Status || container.status || 'Unknown';
  const ports = container.Ports || [];

  const isRunning = status.toLowerCase().includes('running');

  const isActionLoading = (action) => {
    return loadingActions[`${containerName}_${action}`] || false;
  };

  const actions = [
    {
      icon: Play,
      label: 'Start Container',
      color: 'success',
      action: 'start',
      disabled: isRunning || isActionLoading('start'),
      description: 'Start the container if it\'s stopped',
      loading: isActionLoading('start')
    },
    {
      icon: Square,
      label: 'Stop Container',
      color: 'warning',
      action: 'stop',
      disabled: !isRunning || isActionLoading('stop'),
      description: 'Gracefully stop the container',
      loading: isActionLoading('stop')
    },
    {
      icon: RotateCcw,
      label: 'Restart Container',
      color: 'secondary',
      action: 'restart',
      disabled: !isRunning || isActionLoading('restart'),
      description: 'Restart the running container',
      loading: isActionLoading('restart')
    },
    {
      icon: Trash2,
      label: 'Remove Container',
      color: 'danger',
      action: 'remove',
      disabled: isActionLoading('remove'),
      description: 'Remove the container (cannot be undone)',
      loading: isActionLoading('remove')
    }
  ];

  const openWebInterface = async () => {
    const exactName = containerName;
    let targetUrl = container.public_url || (container.web_url && !container.web_url.includes('localhost') && !container.web_url.includes('127.0.0.1') ? container.web_url : null);
    
    if (!targetUrl) {
      try {
        const response = await apiService.getContainerWebUrl(exactName);
        if (response?.data?.success && response.data.data?.web_url) {
          const resolved = response.data.data.web_url;
          if (!resolved.includes('localhost') && !resolved.includes('127.0.0.1')) {
            targetUrl = resolved;
          }
        }
      } catch (err) {
        console.warn(`Could not resolve public web URL for ${exactName}:`, err);
      }
    }

    if (targetUrl) {
      console.log(`🌐 Opening public web interface for ${exactName}: ${targetUrl}`);
      window.open(targetUrl, '_blank', 'noopener,noreferrer');
    } else {
      console.info(`ℹ️ Container "${exactName}" is an internal service without a configured public application URL.`);
      alert(`ℹ️ Container "${exactName}" is an internal service without a configured public application URL.`);
    }
  };

  const downloadDockerImage = async () => {
    const exactName = containerName;
    try {
      setIsDownloading(true);
      const response = await apiService.downloadContainerImage(exactName);
      if (response?.data?.success && response.data.data?.download_url) {
        const { download_url, filename } = response.data.data;
        const link = document.createElement('a');
        link.href = download_url;
        link.setAttribute('download', filename || `${exactName}.tar`);
        document.body.appendChild(link);
        link.click();
        link.remove();
      } else {
        alert(`⚠️ Export Failed: ${response?.data?.error || 'Unknown error exporting image'}`);
      }
    } catch (err) {
      const msg = err.response?.data?.error || err.message || 'Error exporting container image';
      alert(`⚠️ Export Error:\n\n${msg}`);
    } finally {
      setIsDownloading(false);
    }
  };

  return (
    <div className="container-actions-panel">
      <div className="panel-header">
        <h3>Container Actions</h3>
        <div className="status-indicator">
          <div className={`status-dot ${isRunning ? 'running' : 'stopped'}`}></div>
          <span>{isRunning ? 'Running' : 'Stopped'}</span>
        </div>
      </div>

      <div className="action-buttons">
        {actions.map((action, index) => {
          const Icon = action.icon;
          return (
            <div key={index} className="action-item">
              <button
                className={`btn btn-${action.color} action-btn`}
                onClick={() => onAction(action.action, containerName)}
                disabled={action.disabled}
                title={action.description}
              >
                {action.loading ? (
                  <Loader size={18} className="spin" />
                ) : (
                  <Icon size={18} />
                )}
                <span>
                  {action.loading ? `${action.label}...` : action.label}
                </span>
              </button>
              <div className="action-description">
                {action.description}
              </div>
            </div>
          );
        })}

        {(ports.length > 0 || container.public_url || container.web_url) && isRunning && (
          <div className="action-item">
            <button
              className="btn btn-primary action-btn"
              onClick={openWebInterface}
              title="Open web interface in browser"
            >
              <Globe size={18} />
              <span>Open Web UI</span>
            </button>
            <div className="action-description">
              Open container's web interface
            </div>
          </div>
        )}

        <div className="action-item">
          <button
            className="btn btn-secondary action-btn"
            onClick={downloadDockerImage}
            disabled={isDownloading}
            title="Download Docker image (.tar archive via S3)"
          >
            {isDownloading ? (
              <Loader size={18} className="spin" />
            ) : (
              <Download size={18} />
            )}
            <span>{isDownloading ? 'Saving Image...' : 'Download Image (.tar)'}</span>
          </button>
          <div className="action-description">
            Export container image as .tar archive via S3
          </div>
        </div>
      </div>
      
      {/* ... rest of the component remains the same ... */}
    </div>
  );
};

export default ContainerActions;