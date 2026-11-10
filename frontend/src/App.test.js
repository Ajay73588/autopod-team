import { render, screen } from '@testing-library/react';
import App from './App';

jest.mock('./services/api', () => ({
  apiService: {
    getContainerStatus: jest.fn().mockResolvedValue({ data: { success: true, data: [] } }),
    getContainers: jest.fn().mockResolvedValue({ data: { success: true, data: [] } }),
    getAppNodes: jest.fn().mockResolvedValue({ data: { success: true, nodes: [] } }),
    getVersionsFor: jest.fn().mockResolvedValue({ data: { success: true, versions: [] } }),
    getDeployments: jest.fn().mockResolvedValue({ data: { success: true, deployments: [] } }),
  },
}));

test('renders the AutoPod Cloud sidebar navigation without crashing', () => {
  render(<App />);
  expect(screen.getAllByText(/dashboard/i).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/deployments/i).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/rollback/i).length).toBeGreaterThan(0);
});

