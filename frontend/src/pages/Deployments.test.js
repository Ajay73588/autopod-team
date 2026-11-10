import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import Deployments from './Deployments';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('Deployments Page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders Target Node column and distinguishes orchestrator from worker rollouts', async () => {
    const mockDeployments = [
      {
        id: 3,
        version_id: 2,
        git_commit: '7ce6ea57295a116c29998d3dd925c1c6f2b9532b',
        image_tag: '822897118262.dkr.ecr.us-east-1.amazonaws.com/autopod-sweet-treats-bakery:7ce6ea5-20261028-132454',
        status: 'success',
        triggered_by: 'webhook',
        target_node: '10.0.1.107',
        started_at: '2026-10-28 13:24:56',
        finished_at: '2026-10-28 13:25:08',
        message: null,
      },
      {
        id: 2,
        version_id: 2,
        git_commit: '7ce6ea57295a116c29998d3dd925c1c6f2b9532b',
        image_tag: '822897118262.dkr.ecr.us-east-1.amazonaws.com/autopod-sweet-treats-bakery:7ce6ea5-20261028-132454',
        status: 'success',
        triggered_by: 'webhook',
        target_node: null,
        started_at: '2026-10-28 13:24:54',
        finished_at: '2026-10-28 13:25:08',
        message: 'Rolling deployment to 1 node(s) succeeded.',
      },
    ];

    apiService.getDeployments.mockResolvedValueOnce({
      data: { success: true, data: mockDeployments },
    });

    render(<Deployments />);

    // Header check
    expect(screen.getByText('Loading deployment history...')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('Target')).toBeInTheDocument();
    });

    // Worker node pill check
    expect(screen.getByText(/10\.0\.1\.107/)).toBeInTheDocument();

    // Orchestrator pipeline pill check
    expect(screen.getByText(/Pipeline/)).toBeInTheDocument();

    // Commit format check (7 characters)
    expect(screen.getAllByText('7ce6ea5').length).toBe(2);
  });
});

