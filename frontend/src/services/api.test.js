import { apiService, API_BASE_URL } from './api';

jest.mock('axios', () => {
  const mAxiosInstance = {
    get: jest.fn(),
    post: jest.fn(),
    interceptors: {
      request: { use: jest.fn() },
      response: { use: jest.fn() },
    },
    defaults: { headers: { common: {} } },
  };
  return {
    create: jest.fn(() => mAxiosInstance),
    ...mAxiosInstance,
  };
});

describe('apiService methods and configuration', () => {
  test('API_BASE_URL is defined as a string', () => {
    expect(typeof API_BASE_URL).toBe('string');
  });

  test('getAllContainersHealth is exported and callable', () => {
    expect(typeof apiService.getAllContainersHealth).toBe('function');
  });

  test('getAllContainersNetwork is exported and callable', () => {
    expect(typeof apiService.getAllContainersNetwork).toBe('function');
  });

  test('getAllNetworks alias is exported and callable', () => {
    expect(typeof apiService.getAllNetworks).toBe('function');
  });
});
