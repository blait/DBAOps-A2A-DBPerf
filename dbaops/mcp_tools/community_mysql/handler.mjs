/**
 * benborla/mcp-server-mysql stdio adapter (read-only by default).
 *
 * MYSQL_HOST/PORT/DB env 는 terraform 으로 주입.
 * MYSQL_USER/MYSQL_PASS 는 Secrets Manager 에서 startup 시 fetch.
 * ALLOW_INSERT/UPDATE/DELETE_OPERATION 모두 false (default RO 유지).
 */

import {
  BedrockAgentCoreGatewayTargetHandler,
  StdioServerAdapterRequestHandler,
} from "@aws/run-mcp-servers-with-aws-lambda";
import {
  SecretsManagerClient,
  GetSecretValueCommand,
} from "@aws-sdk/client-secrets-manager";

let _eventHandler = null;
let _cachedCreds = null;

async function fetchCreds() {
  if (_cachedCreds) return _cachedCreds;
  const sm = new SecretsManagerClient({});
  const resp = await sm.send(
    new GetSecretValueCommand({ SecretId: process.env.MYSQL_SECRET_ARN })
  );
  _cachedCreds = JSON.parse(resp.SecretString || "{}");
  return _cachedCreds;
}

async function ensureHandler() {
  if (_eventHandler) return _eventHandler;
  const creds = await fetchCreds();

  const env = {
    MYSQL_HOST: process.env.MYSQL_HOST,
    MYSQL_PORT: process.env.MYSQL_PORT || "3306",
    MYSQL_USER: creds.username || "dbaops_admin",
    MYSQL_PASS: creds.password,
    MYSQL_DB:   process.env.MYSQL_DB || "dbaops",
    ALLOW_INSERT_OPERATION: "false",
    ALLOW_UPDATE_OPERATION: "false",
    ALLOW_DELETE_OPERATION: "false",
    PATH: process.env.PATH || "",
    NODE_PATH: process.env.NODE_PATH || "",
    HOME: "/tmp",
  };

  const serverParams = {
    command: "node",
    args: ["/var/task/node_modules/@benborla29/mcp-server-mysql/dist/index.js"],
    env,
  };

  const requestHandler = new StdioServerAdapterRequestHandler(serverParams);
  _eventHandler = new BedrockAgentCoreGatewayTargetHandler(requestHandler);
  return _eventHandler;
}

export const handler = async (event, context) => {
  const eventHandler = await ensureHandler();
  return eventHandler.handle(event, context);
};
