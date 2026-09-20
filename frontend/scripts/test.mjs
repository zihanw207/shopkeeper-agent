// 使用现有 TypeScript 编译器和 Node 测试运行器，无需安装额外测试框架。
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = mkdtempSync(join(tmpdir(), "shopkeeper-frontend-tests-"));
try {
  writeFileSync(join(output, "package.json"), '{"type":"commonjs"}');
  const compile = spawnSync(process.execPath, [
    "node_modules/typescript/bin/tsc", "--module", "commonjs", "--moduleResolution", "node",
    "--target", "ES2022", "--lib", "ES2022,DOM", "--types", "node", "--strict",
    "--esModuleInterop", "--skipLibCheck", "--outDir", output, "--rootDir", ".",
    "tests/conversationManager.test.ts",
  ], { cwd: root, stdio: "inherit" });
  if (compile.status !== 0) process.exitCode = compile.status ?? 1;
  else {
    const tests = spawnSync(process.execPath, ["--test", join(output, "tests/conversationManager.test.js")], { stdio: "inherit" });
    process.exitCode = tests.status ?? 1;
  }
} finally {
  rmSync(output, { recursive: true, force: true });
}
