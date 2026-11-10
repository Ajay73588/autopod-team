import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import Networking from './Networking';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('Networking Page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('calls getAllContainersNetwork and getNetworkStats without crashing', async () => {
    apiService.getAllContainersNetwork.mockResolvedValueOnce({
      data: {
        success: true,
        data: {
          'autopod-sweet-treats-bakery-container': {
            ip_address: '172.18.0.3',
            gateway: '172.18.0.1',
            ports: [{ host_port: '8081', container_port: '80', protocol: 'tcp' }],
            networks: ['autopod-network'],
          },
        },
      },
    });

    apiService.getNetworkStats.mockResolvedValueOnce({
      data: {
        success: true,
        data: {
          total_containers: 1,
          containers_with_ports: 1,
          total_ports_exposed: 1,
          unique_networks: 1,
        },
      },
    });

    render(<Networking />);

    await waitFor(() => {
      expect(screen.getByText('autopod-sweet-treats-bakery-container')).toBeInTheDocument();
    });

    expect(screen.getByText('172.18.0.3')).toBeInTheDocument();
  });

  test('handles API errors with user-visible error banner', async () => {
    apiService.getAllContainersNetwork.mockRejectedValueOnce(new Error('Failed to fetch network topology'));
    apiService.getNetworkStats.mockResolvedValueOnce({ data: { success: true, data: {} } });

    render(<Networking />);

    await waitFor(() => {
      expect(screen.getByText("Couldn't load network information")).toBeInTheDocument();
    });

    expect(screen.getByText('Failed to fetch network topology')).toBeInTheDocument();
  });
});

