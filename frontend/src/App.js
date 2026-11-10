import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import SplashScreen from './components/SplashScreen';
import Dashboard from './pages/Dashboard';
import ContainerManagement from './pages/ContainerManagement';
import ImageRegistry from './pages/ImageRegistry';
import Networking from './pages/Networking';
import Logs from './pages/Logs';
import HealthMonitoring from './pages/HealthMonitoring';
import Deployments from './pages/Deployments';
import Rollback from './pages/Rollback';
import Fleet from './pages/Fleet';
import Layout from './components/Layout';
import './styles/App.css';

function App() {
  return (
    <Router>
      <div className="App">
        <SplashScreen />
        <Layout>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/containers" element={<ContainerManagement />} />
            <Route path="/images" element={<ImageRegistry />} />
            <Route path="/deployments" element={<Deployments />} />
            <Route path="/rollback" element={<Rollback />} />
            <Route path="/fleet" element={<Fleet />} />
            <Route path="/networking" element={<Networking />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/health" element={<HealthMonitoring />} />
          </Routes>
        </Layout>
      </div>
    </Router>
  );
}

export default App;