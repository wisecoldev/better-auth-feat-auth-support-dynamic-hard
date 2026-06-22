// Driver-generated validation params helper.
// DO NOT MODIFY. Reads test parameters from an immutable JSON file.
import * as fs from 'fs';

const PARAMS_PATH = process.env.VALIDATION_PARAMS!;
if (!PARAMS_PATH) {
  throw new Error('VALIDATION_PARAMS environment variable not set');
}

let _params: any = null;

export function loadParams(): any {
  if (!_params) {
    _params = JSON.parse(fs.readFileSync(PARAMS_PATH, 'utf-8'));
  }
  return _params;
}

export function getTestCases(): Array<{inputs: any; expected: any}> {
  return loadParams().test_cases || [];
}
