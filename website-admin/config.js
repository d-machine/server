const API_BASE_URL = window.API_BASE_URL || (
  location.hostname === 'localhost' || location.hostname === '127.0.0.1'
    ? 'http://localhost:8000'
    : 'https://arthdeskapi.ashokitservices.com'
);
