import { apiService } from '../services/api';

export const testWebhook = async () => {
  try {
    console.log('Testing webhook endpoint...');
    
    const testData = {
      repository: {
        clone_url: "https://github.com/nginx/nginx.git",
        name: "test-app"
      },
      after: "test-deploy-" + Date.now()
    };
    
    console.log('Sending test data:', testData);
    
    const response = await apiService.triggerWebhook(testData);
    console.log('Webhook response:', response);
    
    return response;
  } catch (error) {
    console.error('Webhook test failed:', error);
    throw error;
  }
};

// Run this in browser console to test
window.testWebhook = testWebhook;