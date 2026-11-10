import React, { useState, useEffect } from 'react';
import { apiService } from '../services/api';
import '../styles/Networking.css';

const Networking = () => {
  const [networkData, setNetworkData] = useState({});
  const [networkStats, setNetworkStats] = useState({});
  const [showStats, setShowStats] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchNetworkData();
    const interval = setInterval(fetchNetworkData, 10000);
    return () => clearInterval(interval);
  }, []);

  const fetchNetworkData = async () => {
    try {
      const [networkRes, statsRes] = await Promise.all([
        apiService.getAllContainersNetwork(),
        apiService.getNetworkStats()
      ]);

      if (networkRes.data && networkRes.data.success) {
        setNetworkData(networkRes.data.data || {});
      }

      if (statsRes.data && statsRes.data.success) {
        setNetworkStats(statsRes.data.data || {});
      }
      setError(null);
    } catch (err) {
      console.error('Error fetching network data:', err);
      setError(err.message || 'Failed to fetch network data');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return <div className="loading">Loading network information...</div>;
  }

  return (
    <div className="networking">
      <div className="page-header">
        <h1>🌐 Container Networking</h1>
        <p>Port mappings, network connections, and traffic information</p>
      </div>

      <div className="network-actions">
        <button className="btn btn-secondary" onClick={fetchNetworkData}>
          <span className="btn-icon">🔄</span>
          <span className="btn-text">Refresh Network</span>
        </button>
        <button className="btn btn-secondary" onClick={() => setShowStats(!showStats)}>
          <span className="btn-icon">📊</span>
          <span className="btn-text">Network Stats</span>
        </button>
      </div>

      {error && (
        <div className="empty-state error" style={{ marginBottom: '1.5rem' }}>
          <div className="empty-icon">⚠️</div>
          <h3>Couldn't load network information</h3>
          <p>{error}</p>
        </div>
      )}

      {showStats && (
        <div className="network-stats">
          <div className="stats-row">
            <div className="stat-item">
              <span className="stat-label">Total Containers</span>
              <span className="stat-value">{networkStats.total_containers || 0}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">With Ports</span>
              <span className="stat-value">{networkStats.containers_with_ports || 0}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Total Ports</span>
              <span className="stat-value">{networkStats.total_ports_exposed || 0}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Networks</span>
              <span className="stat-value">{networkStats.unique_networks || 0}</span>
            </div>
          </div>
        </div>
      )}

      <div className="network-grid">
        {Object.keys(networkData).length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">🌐</div>
            <h3>No Network Data</h3>
            <p>No containers with network information found.</p>
          </div>
        ) : (
          Object.entries(networkData).map(([containerName, data]) => (
            <div key={containerName} className="network-card">
              <div className="network-header">
                <div className="network-title">
                  <span className="network-icon">🐳</span>
                  <h3>{containerName}</h3>
                </div>
                <div className="network-badge">
                  {data.ports && data.ports.length > 0 ? (
                    <span className="badge-ports">{data.ports.length} Port{data.ports.length !== 1 ? 's' : ''}</span>
                  ) : (
                    <span className="badge-no-ports">No Ports</span>
                  )}
                </div>
              </div>

              <div className="network-details">
                <div className="detail-row">
                  <span className="detail-label">IP Address:</span>
                  <span className="detail-value">{data.ip_address || 'N/A'}</span>
                </div>
                <div className="detail-row">
                  <span className="detail-label">Gateway:</span>
                  <span className="detail-value">{data.gateway || 'N/A'}</span>
                </div>
              </div>

              {data.ports && data.ports.length > 0 && (
                <div className="ports-section">
                  <h4>Port Mappings</h4>
                  <div className="ports-list">
                    {data.ports.map((port, index) => (
                      <div key={index} className="port-item">
                        <div className="port-mapping">
                          <span className="port-container">{port.container_port}</span>
                          <span className="port-arrow">→</span>
                          <span className="port-host">{port.host_ip}:{port.host_port}</span>
                        </div>
                        {port.url && (
                          <a href={port.url} target="_blank" rel="noopener noreferrer" className="port-link">
                            <span className="link-icon">🔗</span>
                            Open
                          </a>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default Networking;