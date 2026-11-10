import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import DeploymentSection from './DeploymentSection';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('DeploymentSection Component', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders deployment form and handles successful deployment with guided webhook onboarding', async () => {
    apiService.deployApplication.mockResolvedValueOnce({
      data: {
        status: 'success',
        message: 'Deployed successfully to worker fleet',
        data: {
          container_name: 'autopod-crypto-price-analysis-dashboard-container',
          port: 8082,
          public_url: 'http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/',
        },
      },
    });

    apiService.getWebhookDeliveries.mockResolvedValueOnce({
      data: {
        success: true,
        data: [
          {
            id: 1,
            delivery_id: 'del-12345-67890',
            event_type: 'ping',
            received_at: '2026-11-01 01:30:00',
          },
        ],
      },
    });

    render(<DeploymentSection />);

    expect(screen.getByText(/Quick Deploy/i)).toBeInTheDocument();

    const urlInput = screen.getByLabelText(/GitHub Repository URL/i);
    fireEvent.change(urlInput, {
      target: { value: 'https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git' },
    });

    const deployBtn = screen.getByRole('button', { name: /Deploy Now/i });
    fireEvent.click(deployBtn);

    await waitFor(() => {
      expect(apiService.deployApplication).toHaveBeenCalledWith({
        repository: {
          clone_url: 'https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git',
          name: 'crypto-price-analysis-dashboard',
        },
        branch: 'main',
      });
    });

    // Check webhook onboarding card rendered
    await waitFor(() => {
      expect(screen.getByText(/GitHub Continuous Deployment/i)).toBeInTheDocument();
      expect(screen.getByDisplayValue(/https:\/\/3-239-41-33\.sslip\.io\/webhook/i)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /Open Application/i })).toBeInTheDocument();
    });

    // Check verify deliveries button
    const verifyBtn = screen.getByRole('button', { name: /Check Webhook Delivery Status/i });
    fireEvent.click(verifyBtn);

    await waitFor(() => {
      expect(apiService.getWebhookDeliveries).toHaveBeenCalled();
      expect(screen.getByText(/Webhook endpoint verified! Recorded deliveries:/i)).toBeInTheDocument();
      expect(screen.getByText(/del-12345-6789/i)).toBeInTheDocument();
    });

    // Check dismiss button
    const dismissBtn = screen.getByRole('button', { name: /Dismiss deployment card/i });
    fireEvent.click(dismissBtn);

    await waitFor(() => {
      expect(screen.queryByText(/GitHub Continuous Deployment/i)).not.toBeInTheDocument();
    });
  });
});
