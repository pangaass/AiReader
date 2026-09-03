const { app } = require('electron');
const { saveSettings } = require('./settings-store');

let input = '';
if (process.stdin.isTTY && process.stdin.setRawMode) process.stdin.setRawMode(true);
process.stdin.setEncoding('utf8');
const inputReady = new Promise((resolve) => {
  process.stdin.on('data', (chunk) => {
    input += chunk;
    if (input.includes('\n')) resolve(input);
  });
  process.stdin.on('end', () => resolve(input));
});

app.whenReady().then(async () => {
  try {
    const settings = JSON.parse((await inputReady).trim());
    await saveSettings(settings);
    process.stdout.write('LLM 配置已安全保存。\n');
    app.quit();
  } catch (error) {
    process.stderr.write(`配置失败：${error.message}\n`);
    app.exit(1);
  }
});
