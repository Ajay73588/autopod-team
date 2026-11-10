import React, { useState, useEffect } from 'react';
import { apiService } from '../api/api';

const ContainerList = () => {
  const [containers, setContainers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchContainers = async () => {
    try {
      setLoading(true);
      setError(null);
      
      console.log('🔄 Fetching containers...');
      const response = await apiService.getContainers();
      console.log('📦 Containers response:', response);
      
      if (response.data.success) {
        setContainers(response.data.data || []);
        console.log(`✅ Loaded ${response.data.data?.length || 0} containers`);
      } else {
        throw new Error(response.data.error || 'Failed to fetch containers');
      }
    } catch (err) {
      console.error('❌ Error fetching containers:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleContainerAction = async (action, containerName) => {
    try {
      console.log(`🔄 Performing ${action} on ${containerName}`);
      
      let response;
      switch (action) {
        case 'start':
          response = await apiService.startContainer(containerName);
          break;
        case 'stop':
          response = await apiService.stopContainer(containerName);
          break;
        case 'restart':
          response = await apiService.restartContainer(containerName);
          break;
        case 'remove':
          response = await apiService.removeContainer(containerName);
          break;
        default:
          throw new Error(`Unknown action: ${action}`);
      }
      
      if (response.data.success) {
        console.log(`✅ ${action} successful for ${containerName}`);
        // Refresh containers list
        await fetchContainers();
      } else {
        throw new Error(response.data.error || `Failed to ${action} container`);
      }
    } catch (err) {
      console.error(`❌ Error performing ${action}:`, err);
      setError(err.message);
    }
  };

  useEffect(() => {
    fetchContainers();
  }, []);

  if (loading) {
    return (
      <div style={{ padding: '20px', textAlign: 'center' }}>
        <div>Loading containers...</div>
        <button onClick={fetchContainers} style={{ marginTop: '10px' }}>
          Force Refresh
        </button>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: '20px', color: 'red' }}>
        <h3>Error Loading Containers</h3>
        <p>{error}</p>
        <button onClick={fetchContainers} style={{ marginTop: '10px' }}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div style={{ padding: '20px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
        <h2>Containers ({containers.length})</h2>
        <div>
          <button onClick={fetchContainers} style={{ marginRight: '10px' }}>
            Refresh
          </button>
          <button onClick={() => apiService.syncContainers().then(fetchContainers)}>
            Sync with Podman
          </button>
        </div>
      </div>

      {containers.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px' }}>
          <h3>No containers found</h3>
          <p>Make sure Podman is running and has containers</p>
          <button onClick={fetchContainers}>Check Again</button>
        </div>
      ) : (
        <div style={{ display: 'grid', gap: '15px' }}>
          {containers.map((container, index) => {
            const name = container.Names ? container.Names[0] : 'Unknown';
            const status = container.Status || 'Unknown';
            const state = container.State || 'Unknown';
            const isRunning = state.toLowerCase() === 'running';
            
            return (
              <div key={index} style={{ 
                border: '1px solid #ddd', 
                borderRadius: '8px', 
                padding: '15px',
                backgroundColor: isRunning ? '#f0f9ff' : '#f8f9fa'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <h4 style={{ margin: '0 0 5px 0' }}>{name}</h4>
                    <div style={{ display: 'flex', gap: '15px', fontSize: '14px', color: '#666' }}>
                      <span>Status: {status}</span>
                      <span>State: {state}</span>
                      <span>Image: {container.Image || 'N/A'}</span>
                    </div>
                  </div>
                  
                  <div style={{ display: 'flex', gap: '10px' }}>
                    {!isRunning && (
                      <button 
                        onClick={() => handleContainerAction('start', name)}
                        style={{ backgroundColor: '#10b981', color: 'white' }}
                      >
                        Start
                      </button>
                    )}
                    {isRunning && (
                      <>
                        <button 
                          onClick={() => handleContainerAction('stop', name)}
                          style={{ backgroundColor: '#ef4444', color: 'white' }}
                        >
                          Stop
                        </button>
                        <button 
                          onClick={() => handleContainerAction('restart', name)}
                          style={{ backgroundColor: '#f59e0b', color: 'white' }}
                        >
                          Restart
                        </button>
                      </>
                    )}
                    <button 
                      onClick={() => handleContainerAction('remove', name)}
                      style={{ backgroundColor: '#6b7280', color: 'white' }}
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default ContainerList;