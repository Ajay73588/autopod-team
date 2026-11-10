import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import HealthMonitoring from './HealthMonitoring';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('HealthMonitoring Page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('calls getAllContainersHealth and renders container health cards', async () => {
    apiService.getAllContainersHealth.mockResolvedValueOnce({
      data: {
        success: true,
        data: {
          'autopod-sweet-treats-bakery-container': {
            health: { status: 'healthy' },
            resources: { cpu_percent: '0.5%', memory_usage: '24MB' },
          },
        },
      },
    });

    render(<HealthMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('autopod-sweet-treats-bakery-container')).toBeInTheDocument();
    });

    expect(screen.getByText('HEALTHY')).toBeInTheDocument();
    expect(screen.getByText('0.5%')).toBeInTheDocument();
  });

  test('handles API errors with user-visible error message', async () => {
    apiService.getAllContainersHealth.mockRejectedValueOnce(new Error('Network error connecting to backend'));

    render(<HealthMonitoring />);

    await waitFor(() => {
      expect(screen.getByText("Couldn't load health data")).toBeInTheDocument();
    });

    expect(screen.getByText('Network error connecting to backend')).toBeInTheDocument();
  });
});

