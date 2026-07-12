// In production, set window.API_BASE_URL before this script, or rely on the default.
const API_BASE_URL = window.API_BASE_URL || (
  location.hostname === 'localhost' || location.hostname === '127.0.0.1'
    ? 'http://localhost:8000'
    : 'https://arthdeskapi.ashokitservices.com'
);
