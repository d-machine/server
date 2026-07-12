'use strict';

function computePrice(gains) {
  return Math.min(10000, 1000 + 0.0002 * Math.max(0, gains - 1000000));
}

function updatePrice() {
  const raw = parseFloat(document.getElementById('gains-input').value) || 0;
  const price = computePrice(raw);
  document.getElementById('price-display').textContent =
    '₹' + Math.round(price).toLocaleString('en-IN');
}
window.updatePrice = updatePrice;
