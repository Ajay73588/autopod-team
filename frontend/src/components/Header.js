import React from 'react';
import { Bell, Settings } from 'lucide-react';
import '../styles/Header.css';

const Header = () => {
  return (
    <header className="header">
      <div className="header-content">
        <div className="header-left">
          <div className="logo">
            <span className="logo-icon">🚀</span>
            <h1>AutoPod</h1>
          </div>
          <p className="header-subtitle">Intelligent Container Dashboard</p>
        </div>
        <div className="header-right">
          <div className="status-indicator">
            <span className="status-dot"></span>
            <span className="status-text">System Active</span>
          </div>
          <div className="header-actions">
            <button className="icon-btn">
              <Bell size={18} />
            </button>
            <button className="icon-btn">
              <Settings size={18} />
            </button>
          </div>
        </div>
      </div>
    </header>
  );
};

export default Header;