const request = require('supertest');
const baseUrl = 'https://9l4b3273sc.execute-api.us-east-1.amazonaws.com/Prod';

describe('API Integration Tests', () => {
  test('POST /upload should return 200 with valid filename', async () => {
    const response = await request(baseUrl)
      .post('/upload')
      .send({ filename: 'test.pdf' });
    expect(response.status).toBe(200);
    expect(response.body).toHaveProperty('upload_url');
    expect(response.body).toHaveProperty('upload_id');
  });

  test('POST /upload should return 400 without filename', async () => {
    const response = await request(baseUrl)
      .post('/upload')
      .send({});
    expect(response.status).toBe(400);
    expect(response.body).toHaveProperty('message', 'Filename is required');
  });
});