// Driver-generated validation params helper.
// DO NOT MODIFY. Reads test parameters from an immutable JSON file.
const fs = require('fs');
const path = require('path');

const PARAMS_PATH = process.env.VALIDATION_PARAMS;
if (!PARAMS_PATH) {
  throw new Error('VALIDATION_PARAMS environment variable not set');
}

let _params = null;

function loadParams() {
  if (!_params) {
    _params = JSON.parse(fs.readFileSync(PARAMS_PATH, 'utf-8'));
  }
  return _params;
}

function getTestCases() {
  return loadParams().test_cases || [];
}

module.exports = { loadParams, getTestCases };
