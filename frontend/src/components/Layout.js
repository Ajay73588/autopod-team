import React from 'react';
import Header from './Header';
import Sidebar from './Sidebar';
import '../styles/Layout.css';

const Layout = ({ children }) => {
  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-content">
        <Header />
        <main className="main-content">
          {children}
        </main>
      </div>
    </div>
  );
};

export default Layout;