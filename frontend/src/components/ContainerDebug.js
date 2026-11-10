import React, { useState, useEffect } from 'react';
import { apiService, checkBackendConnection, testApiEndpoints } from '../api/api';

const ContainerDebug = () => {
  const [debugInfo, setDebugInfo] = useState({});
  const [containers, setContainers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const debugConnection = async () => {
    try {
      console.log('🔍 Starting connection debug...');
      setLoading(true);
      setError(null);
      
      // 1. Check backend connection
      const connection = await checkBackendConnection();
      console.log('🔍 Backend connection:', connection);
      
      // 2. Test all API endpoints
      const apiTest = await testApiEndpoints();
      console.log('🔍 API test results:', apiTest);
      
      // 3. Try to fetch containers directly with better error handling
      let containersResponse;
      let containersData = [];
      
      try {
        containersResponse = await apiService.getContainers();
        console.log('🔍 Raw containers response:', containersResponse);
        
        // Handle different response structures
        if (containersResponse.data) {
          if (containersResponse.data.success && containersResponse.data.data) {
            // Standard structure: { success: true, data: [...] }
            containersData = containersResponse.data.data;
          } else if (Array.isArray(containersResponse.data)) {
            // Direct array structure: [...]
            containersData = containersResponse.data;
          } else if (containersResponse.data.containers) {
            // Alternative structure: { containers: [...] }
            containersData = containersResponse.data.containers;
          }
        }
        
        console.log('🔍 Processed containers data:', containersData);
      } catch (containerError) {
        console.error('❌ Containers endpoint error:', containerError);
        containersResponse = { error: containerError.message };
      }
      
      // 4. Try status endpoint
      let statusResponse;
      try {
        statusResponse = await apiService.getContainerStatus();
        console.log('🔍 Status response:', statusResponse);
      } catch (statusError) {
        console.error('❌ Status endpoint error:', statusError);
        statusResponse = { error: statusError.message };
      }
      
      setDebugInfo({
        connection,
        apiTest,
        containers: containersResponse,
        status: statusResponse,
        processedContainers: containersData
      });
      
      setContainers(containersData);
      
    } catch (err) {
      console.error('❌ Debug error:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    debugConnection();
  }, []);

  if (loading) return (
    <div style={{ padding: '20px', textAlign: 'center' }}>
      <div>Loading debug information...</div>
      <div>Checking backend connection and API endpoints</div>
    </div>
  );

  return (
    <div style={{ padding: '20px', fontFamily: 'monospace' }}>
      <h2>Container Data Debug</h2>
      
      {error && (
        <div style={{ color: 'red', marginBottom: '20px', padding: '10px', backgroundColor: '#ffe6e6' }}>
          <strong>Error:</strong> {error}
        </div>
      )}
      
      <div style={{ marginBottom: '20px' }}>
        <button 
          onClick={debugConnection} 
          style={{ padding: '10px', margin: '5px', backgroundColor: '#007bff', color: 'white', border: 'none', borderRadius: '4px' }}
        >
          Refresh Debug
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
        {/* Connection Status */}
        <div style={{ border: '1px solid #ccc', padding: '15px', borderRadius: '5px' }}>
          <h3>Connection Status</h3>
          <pre>{JSON.stringify(debugInfo.connection, null, 2)}</pre>
        </div>

        {/* API Test Results */}
        <div style={{ border: '1px solid #ccc', padding: '15px', borderRadius: '5px' }}>
          <h3>API Test Results</h3>
          <pre>{JSON.stringify(debugInfo.apiTest, null, 2)}</pre>
        </div>

        {/* Raw Containers Response */}
        <div style={{ border: '1px solid #ccc', padding: '15px', borderRadius: '5px', gridColumn: 'span 2' }}>
          <h3>Raw Containers Response</h3>
          <pre>{JSON.stringify(debugInfo.containers, null, 2)}</pre>
        </div>

        {/* Processed Containers */}
        <div style={{ border: '1px solid #ccc', padding: '15px', borderRadius: '5px', gridColumn: 'span 2' }}>
          <h3>Processed Containers ({containers.length} containers)</h3>
          {containers.length === 0 ? (
            <div style={{ color: 'orange', padding: '10px' }}>
              <p>No containers found or unable to parse container data</p>
              <p>Check backend logs and Podman installation</p>
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ backgroundColor: '#f5f5f5' }}>
                  <th style={{ border: '1px solid #ddd', padding: '8px' }}>Name</th>
                  <th style={{ border: '1px solid #ddd', padding: '8px' }}>Status</th>
                  <th style={{ border: '1px solid #ddd', padding: '8px' }}>State</th>
                  <th style={{ border: '1px solid #ddd', padding: '8px' }}>Image</th>
                  <th style={{ border: '1px solid #ddd', padding: '8px' }}>ID</th>
                </tr>
              </thead>
              <tbody>
                {containers.map((container, index) => (
                  <tr key={index}>
                    <td style={{ border: '1px solid #ddd', padding: '8px' }}>
                      {container.Names ? container.Names[0] : 
                       container.name ? container.name : 
                       container.container_name ? container.container_name : 
                       'Unknown'}
                    </td>
                    <td style={{ border: '1px solid #ddd', padding: '8px' }}>
                      {container.Status || container.status || 'Unknown'}
                    </td>
                    <td style={{ border: '1px solid #ddd', padding: '8px' }}>
                      {container.State || container.state || 'Unknown'}
                    </td>
                    <td style={{ border: '1px solid #ddd', padding: '8px' }}>
                      {container.Image || container.image || 'Unknown'}
                    </td>
                    <td style={{ border: '1px solid #ddd', padding: '8px', fontSize: '12px' }}>
                      {container.Id || container.id || 'N/A'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

export default ContainerDebug;