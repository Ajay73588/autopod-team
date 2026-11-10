import React, { useState, useEffect } from 'react';
import { apiService } from '../services/api';
import '../styles/HealthMonitoring.css';

const HealthMonitoring = () => {
  const [healthData, setHealthData] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);

  useEffect(() => {
    fetchHealthData();
    
    let interval;
    if (autoRefresh) {
      interval = setInterval(fetchHealthData, 3000);
    }
    
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoRefresh]);

  const fetchHealthData = async () => {
    try {
      const response = await apiService.getAllContainersHealth();
      if (response.data && response.data.success) {
        setHealthData(response.data.data || {});
        setError(null);
      } else {
        setError(response.data?.error || 'Failed to fetch container health data');
      }
    } catch (err) {
      console.error('Error fetching health data:', err);
      setError(err.message || 'Failed to fetch container health data');
    } finally {
      setLoading(false);
    }
  };

  const getHealthStatusColor = (status) => {
    const statusLower = status.toLowerCase();
    if (statusLower === 'healthy' || statusLower === 'running') return '#10b981';
    if (statusLower === 'unhealthy' || statusLower === 'exited') return '#ef4444';
    if (statusLower === 'starting' || statusLower === 'created') return '#3b82f6';
    return '#94a3b8';
  };

  const getHealthStatusIcon = (status) => {
    const statusLower = status.toLowerCase();
    if (statusLower === 'healthy' || statusLower === 'running') return '✅';
    if (statusLower === 'unhealthy' || statusLower === 'exited') return '❌';
    if (statusLower === 'starting' || statusLower === 'created') return '🔄';
    return '❓';
  };

  if (loading) {
    return <div className="loading">Loading health data...</div>;
  }

  return (
    <div className="health-monitoring">
      <div className="page-header">
        <h1>💚 Health Monitoring</h1>
        <p>Container performance and resource usage</p>
      </div>

      <div className="health-actions">
        <button className="btn btn-secondary" onClick={fetchHealthData}>
          <span className="btn-icon">📊</span>
          <span className="btn-text">Refresh Health</span>
        </button>
        <button 
          className={`btn ${autoRefresh ? 'btn-warning' : 'btn-success'}`}
          onClick={() => setAutoRefresh(!autoRefresh)}
        >
          <span className="btn-icon">{autoRefresh ? '⏹️' : '🔄'}</span>
          <span className="btn-text">{autoRefresh ? 'Stop Auto-Refresh' : 'Auto-Refresh'}</span>
        </button>
      </div>

      {error && (
        <div className="empty-state error" style={{ marginBottom: '1.5rem' }}>
          <div className="empty-icon">⚠️</div>
          <h3>Couldn't load health data</h3>
          <p>{error}</p>
        </div>
      )}

      <div className="health-grid">
        {Object.keys(healthData).length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">💚</div>
            <h3>No Health Data</h3>
            <p>No containers found for health monitoring.</p>
          </div>
        ) : (
          Object.entries(healthData).map(([containerName, data]) => {
            const health = data.health || {};
            const resources = data.resources || {};
            
            return (
              <div 
                key={containerName} 
                className="health-card"
                style={{ borderLeft: `4px solid ${getHealthStatusColor(health.status)}` }}
              >
                <div className="health-header">
                  <div className="health-title">
                    <span className="health-icon">{getHealthStatusIcon(health.status)}</span>
                    <h3>{containerName}</h3>
                  </div>
                  <div className="health-status">
                    <span 
                      className="health-status-badge"
                      style={{ backgroundColor: getHealthStatusColor(health.status) }}
                    >
                      {health.status?.toUpperCase() || 'UNKNOWN'}
                    </span>
                  </div>
                </div>

                <div className="health-metrics">
                  <div className="metric">
                    <span className="metric-label">CPU:</span>
                    <span className="metric-value">{resources.cpu_percent || '0%'}</span>
                  </div>
                  <div className="metric">
                    <span className="metric-label">Memory:</span>
                    <span className="metric-value">{resources.memory_usage || '0B'}</span>
                  </div>
                  <div className="metric">
                    <span className="metric-label">Network:</span>
                    <span className="metric-value">{resources.network_io || '0B'}</span>
                  </div>
                  <div className="metric">
                    <span className="metric-label">Restarts:</span>
                    <span className="metric-value">{resources.restart_count || 0}</span>
                  </div>
                </div>

                <div className="health-actions">
                  <button className="btn btn-success btn-sm">
                    ▶️ Start
                  </button>
                  <button className="btn btn-warning btn-sm">
                    🔄 Restart
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default HealthMonitoring;