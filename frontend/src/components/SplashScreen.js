import React, { useEffect, useState } from 'react';
import '../styles/SplashScreen.css';

const SplashScreen = () => {
  const [visible, setVisible] = useState(true);
  const [startExit, setStartExit] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setStartExit(true);
      // Additional delay before completely removing from DOM
      setTimeout(() => setVisible(false), 800);
    }, 3000);

    return () => clearTimeout(timer);
  }, []);

  if (!visible) return null;

  return (
    <div className={`splash-screen ${startExit ? 'splash-exit' : ''}`}>
      <div className="splash-background">
        <div className="floating-shapes">
          <div className="shape shape-1"></div>
          <div className="shape shape-2"></div>
          <div className="shape shape-3"></div>
          <div className="shape shape-4"></div>
        </div>
        <div className="gradient-overlay"></div>
      </div>
      
      <div className="splash-content">
        <div className="logo-container">
          <div className="splash-icon">🚀</div>
          <div className="glow-effect"></div>
        </div>
        
        <div className="text-content">
          <h1 className="splash-title">
            <span className="title-text">AutoPod</span>
            <span className="title-underline"></span>
          </h1>
          <p className="splash-subtitle">Intelligent Container Management</p>
        </div>

        <div className="splash-loader">
          <div className="loader-dot"></div>
          <div className="loader-dot"></div>
          <div className="loader-dot"></div>
          <div className="loader-dot"></div>
        </div>

        <div className="splash-footer">
          <p className="version-text">Version 2.1.0</p>
          <p className="loading-text">Initializing systems...</p>
        </div>
      </div>
    </div>
  );
};

export default SplashScreen;