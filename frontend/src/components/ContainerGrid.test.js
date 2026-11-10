import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ContainerGrid from './ContainerGrid';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('ContainerGrid Open Web Interface Action', () => {
  let originalOpen;
  let originalAlert;

  beforeEach(() => {
    jest.clearAllMocks();
    originalOpen = window.open;
    originalAlert = window.alert;
    window.open = jest.fn();
    window.alert = jest.fn();
  });

  afterEach(() => {
    window.open = originalOpen;
    window.alert = originalAlert;
  });

  test('opens public ALB URL in new tab for Sweet Treats Bakery container with public_url', () => {
    const albUrl = 'http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/';
    const containers = [
      {
        Id: 'c1',
        Names: ['/autopod-sweet-treats-bakery-container'],
        Status: 'Up 12 hours',
        State: 'running',
        Image: 'autopod-sweet-treats-bakery:latest',
        Ports: [{ PublicPort: '8081', PrivatePort: '80', Type: 'tcp' }],
        public_url: albUrl,
      },
    ];

    render(<ContainerGrid containers={containers} showActions={true} />);

    const openButton = screen.getByRole('button', { name: /open/i });
    expect(openButton).toBeInTheDocument();

    fireEvent.click(openButton);

    expect(window.open).toHaveBeenCalledTimes(1);
    expect(window.open).toHaveBeenCalledWith(albUrl, '_blank', 'noopener,noreferrer');
    expect(window.alert).not.toHaveBeenCalled();
  });

  test('displays informative alert and does NOT open localhost for internal platform container without public_url', async () => {
    const containers = [
      {
        Id: 'c2',
        Names: ['/autopod-backend'],
        Status: 'Up 2 hours',
        State: 'running',
        Image: 'autopod-cloud-backend:latest',
        Ports: [{ PublicPort: '5000', PrivatePort: '5000', Type: 'tcp' }],
        public_url: null,
      },
    ];

    apiService.getContainerWebUrl.mockResolvedValueOnce({
      data: { success: true, data: { web_url: null, has_web_interface: false } },
    });

    render(<ContainerGrid containers={containers} showActions={true} />);

    const openButton = screen.getByRole('button', { name: /open/i });
    expect(openButton).toBeInTheDocument();

    fireEvent.click(openButton);

    await waitFor(() => {
      expect(window.alert).toHaveBeenCalledWith(
        expect.stringContaining('internal service without a configured public application URL')
      );
    });

    expect(window.open).not.toHaveBeenCalled();
  });

  test('asynchronously resolves public URL via apiService when not cached on container', async () => {
    const albUrl = 'http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/';
    const containers = [
      {
        Id: 'c3',
        Names: ['/autopod-sweet-treats-bakery-container'],
        Status: 'Up 12 hours',
        State: 'running',
        Image: 'autopod-sweet-treats-bakery:latest',
        Ports: [{ PublicPort: '8081', PrivatePort: '80', Type: 'tcp' }],
      },
    ];

    apiService.getContainerWebUrl.mockResolvedValueOnce({
      data: { success: true, data: { web_url: albUrl, has_web_interface: true } },
    });

    render(<ContainerGrid containers={containers} showActions={true} />);

    const openButton = screen.getByRole('button', { name: /open/i });
    fireEvent.click(openButton);

    await waitFor(() => {
      expect(window.open).toHaveBeenCalledWith(albUrl, '_blank', 'noopener,noreferrer');
    });
  });
});

