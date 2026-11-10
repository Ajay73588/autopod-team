import React from 'react';
import { RefreshCw, Download, Trash2, Rocket } from 'lucide-react';

const QuickActions = ({ onSync }) => {
  const actions = [
    {
      icon: RefreshCw,
      label: 'Sync Containers',
      color: 'primary',
      onClick: onSync
    },
    {
      icon: Download,
      label: 'Refresh Data',
      color: 'secondary',
      onClick: () => window.location.reload()
    },
    {
      icon: Trash2,
      label: 'Clear Logs',
      color: 'secondary',
      onClick: () => console.log('Clear logs')
    },
    {
      icon: Rocket,
      label: 'Test Webhook',
      color: 'success',
      onClick: () => console.log('Test webhook')
    }
  ];

  return (
    <section className="section">
      <div className="section-header">
        <h2>⚡ Quick Actions</h2>
        <p className="section-subtitle">Manage your containers efficiently</p>
      </div>
      <div className="actions-grid">
        {actions.map((action, index) => {
          const Icon = action.icon;
          return (
            <button
              key={index}
              className={`btn btn-${action.color}`}
              onClick={action.onClick}
            >
              <Icon size={18} />
              <span>{action.label}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
};

export default QuickActions;