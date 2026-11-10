import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { 
  Home, 
  Container, 
  Image, 
  Network, 
  FileText, 
  Activity,
  Server,
  Rocket,
  RotateCcw,
  Layers
} from 'lucide-react';
import '../styles/Sidebar.css';

const Sidebar = () => {
  const location = useLocation();

  const menuItems = [
    { path: '/', icon: Home, label: 'Dashboard' },
    { path: '/containers', icon: Container, label: 'Containers' },
    { path: '/images', icon: Image, label: 'Image Registry' },
    { path: '/deployments', icon: Rocket, label: 'Deployments' },
    { path: '/rollback', icon: RotateCcw, label: 'Rollback' },
    { path: '/fleet', icon: Layers, label: 'Fleet' },
    { path: '/networking', icon: Network, label: 'Networking' },
    { path: '/logs', icon: FileText, label: 'Logs' },
    { path: '/health', icon: Activity, label: 'Health Monitoring' },
  ];

  return (
    <aside className="sidebar">
      <nav className="sidebar-nav">
        {menuItems.map((item) => {
          const Icon = item.icon;
          const isActive = location.pathname === item.path;
          
          return (
            <Link
              key={item.path}
              to={item.path}
              className={`nav-item ${isActive ? 'active' : ''}`}
            >
              <Icon size={20} />
              <span className="nav-label">{item.label}</span>
            </Link>
          );
        })}
      </nav>
      
      <div className="sidebar-footer">
        <div className="system-status">
          <Server size={16} />
          <span>Docker Ready</span>
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;