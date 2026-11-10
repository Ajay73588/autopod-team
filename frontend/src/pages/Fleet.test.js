import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import Fleet from './Fleet';
import { apiService } from '../services/api';

jest.mock('../services/api');

describe('Fleet Page Commit Display', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('correctly renders commit hash from git_commit property', async () => {
    apiService.getApplications.mockResolvedValueOnce({
      data: {
        success: true,
        data: [{ id: 1, name: 'sweet-treats-bakery', repo_url: 'https://github.com/Ajay73588/sweet-treats-bakery', target_port: 8081, health_check_path: '/' }],
      },
    });

    apiService.getAppNodes.mockResolvedValueOnce({
      data: {
        success: true,
        data: [{ id: 1, instance_id: 'i-086803910a4c52091', ip_address: '10.0.1.107', status: 'in_service' }],
      },
    });

    apiService.getVersionsFor.mockResolvedValueOnce({
      data: {
        success: true,
        data: [
          { id: 2, image_tag: 'autopod-sweet-treats-bakery:7ce6ea5', git_commit: '7ce6ea57295a116c29998d3dd925c1c6f2b9532b', is_current: true },
          { id: 1, image_tag: 'autopod-sweet-treats-bakery:f6b32e8', git_commit: 'f6b32e8', is_current: false },
        ],
      },
    });

    render(<Fleet />);

    await waitFor(() => {
      expect(screen.getAllByText('sweet-treats-bakery').length).toBeGreaterThanOrEqual(1);
    });

    // Select application to view versions
    const select = screen.getByRole('combobox');
    fireEvent.change(select, { target: { value: '1' } });

    await waitFor(() => {
      expect(screen.getByText('7ce6ea5')).toBeInTheDocument();
      expect(screen.getByText('f6b32e8')).toBeInTheDocument();
    });
  });
});
