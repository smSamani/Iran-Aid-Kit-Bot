const fs = require("fs");
const path = require("path");

const runtimePath = "/app/dist/index.js";
const receiveStreamFragmentPath = path.join(__dirname, "deepseek_receive_stream.jsfrag");
const createTransStreamFragmentPath = path.join(__dirname, "deepseek_create_trans_stream.jsfrag");

function replaceBetween(text, startMarker, endMarker, replacement) {
  const startIndex = text.indexOf(startMarker);
  const endIndex = text.indexOf(endMarker);
  if (startIndex === -1 || endIndex === -1 || endIndex <= startIndex) {
    throw new Error(`Unable to locate replacement markers: ${startMarker} -> ${endMarker}`);
  }
  return text.slice(0, startIndex) + replacement + "\n" + text.slice(endIndex);
}

let runtime = fs.readFileSync(runtimePath, "utf8");
const receiveStreamFragment = fs.readFileSync(receiveStreamFragmentPath, "utf8").trim();
const createTransStreamFragment = fs.readFileSync(createTransStreamFragmentPath, "utf8").trim();

runtime = runtime.replace(
  'if (!ip) throw new APIException(exceptions_default.API_REQUEST_FAILED, "\\u83B7\\u53D6IP\\u5730\\u5740\\u5931\\u8D25");',
  'if (!ip) { logger_default.warn("IP meta missing, using fallback IP"); ipAddress = "1.1.1.1"; return ipAddress; }'
);

runtime = replaceBetween(
  runtime,
  "async function receiveStream(model, stream, refConvId) {",
  "function createTransStream(model, stream, refConvId, endCallback) {",
  receiveStreamFragment
);

runtime = replaceBetween(
  runtime,
  "function createTransStream(model, stream, refConvId, endCallback) {",
  "function tokenSplit(authorization) {",
  createTransStreamFragment
);

fs.writeFileSync(runtimePath, runtime);
console.log("Patched DeepSeek runtime:", runtimePath);
