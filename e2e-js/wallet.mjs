/**
 * Minimal WalletConnect v2 wallet (responder) using @walletconnect/sign-client.
 *
 * Usage:
 *   export WALLETCONNECT_PROJECT_ID=...
 *   node wallet.mjs --uri-file /tmp/wc-uri.txt
 *
 * Waits for a pairing URI to appear in --uri-file, pairs, approves the session,
 * then answers personal_sign and eth_signTypedData_v4 with a fixed test key.
 * Writes DONE to --done-file when finished handling the expected requests.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SignClient } from "@walletconnect/sign-client";
import { buildApprovedNamespaces, getSdkError } from "@walletconnect/utils";
import { Wallet, verifyMessage, TypedDataEncoder, verifyTypedData } from "ethers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function arg(name, fallback = undefined) {
  const i = process.argv.indexOf(name);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

const projectId = process.env.WALLETCONNECT_PROJECT_ID;
if (!projectId) {
  console.error("WALLETCONNECT_PROJECT_ID is required");
  process.exit(1);
}

const uriFile = arg("--uri-file", path.join(__dirname, "wc-uri.txt"));
const doneFile = arg("--done-file", path.join(__dirname, "wallet-done.txt"));
const expectTyped = process.argv.includes("--typed-data");
// Hardhat/Anvil account #0 — public test key only.
const privateKey =
  process.env.E2E_WALLET_KEY ||
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
const wallet = new Wallet(privateKey);
const address = wallet.address;
const account = `eip155:1:${address}`;

console.log(`[wallet] address=${address}`);
console.log(`[wallet] waiting for URI in ${uriFile}`);

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForUri(file, timeoutMs = 60_000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (fs.existsSync(file)) {
      const uri = fs.readFileSync(file, "utf8").trim();
      if (uri.startsWith("wc:")) return uri;
    }
    await sleep(200);
  }
  throw new Error(`Timed out waiting for pairing URI in ${file}`);
}

function clearFile(file) {
  try {
    fs.unlinkSync(file);
  } catch {
    /* ignore */
  }
}

clearFile(doneFile);

const client = await SignClient.init({
  projectId,
  metadata: {
    name: "e2e-js wallet",
    description: "Automated WalletConnect v2 wallet peer for Python E2E",
    url: "https://github.com/ApeWorX/walletconnect-py",
    icons: [],
  },
});

let signedPersonal = false;
let signedTyped = false;
let sessionTopic = null;

const proposalHandled = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("Timed out on session_proposal")), 60_000);
  client.on("session_proposal", async (proposal) => {
    try {
      console.log(`[wallet] session_proposal id=${proposal.id}`);
      const namespaces = buildApprovedNamespaces({
        proposal: proposal.params,
        supportedNamespaces: {
          eip155: {
            chains: ["eip155:1"],
            methods: [
              "personal_sign",
              "eth_sign",
              "eth_signTypedData",
              "eth_signTypedData_v4",
              "eth_sendTransaction",
              "eth_signTransaction",
            ],
            events: ["chainChanged", "accountsChanged"],
            accounts: [account],
          },
        },
      });
      const { topic, acknowledged } = await client.approve({
        id: proposal.id,
        namespaces,
      });
      sessionTopic = topic;
      console.log(`[wallet] approved session topic=${topic.slice(0, 16)}…`);
      await acknowledged();
      console.log(`[wallet] session acknowledged by dApp`);
      clearTimeout(timer);
      resolve(topic);
    } catch (err) {
      clearTimeout(timer);
      console.error("[wallet] approve failed", err);
      try {
        await client.reject({ id: proposal.id, reason: getSdkError("USER_REJECTED") });
      } catch {
        /* ignore */
      }
      reject(err);
    }
  });
});

client.on("session_request", async (event) => {
  const { id, topic, params } = event;
  const { request, chainId } = params;
  console.log(`[wallet] session_request ${request.method} id=${id} chain=${chainId}`);
  try {
    let result;
    if (request.method === "personal_sign") {
      // params: [message, address] — message may be hex
      const [message, reqAddress] = request.params;
      if (reqAddress.toLowerCase() !== address.toLowerCase()) {
        throw new Error(`Address mismatch: ${reqAddress} != ${address}`);
      }
      let msg = message;
      if (typeof message === "string" && message.startsWith("0x")) {
        msg = Buffer.from(message.slice(2), "hex").toString("utf8");
      }
      result = await wallet.signMessage(msg);
      const recovered = verifyMessage(msg, result);
      console.log(`[wallet] personal_sign ok recovered=${recovered}`);
      signedPersonal = true;
    } else if (
      request.method === "eth_signTypedData_v4" ||
      request.method === "eth_signTypedData"
    ) {
      const [reqAddress, typedData] = request.params;
      if (reqAddress.toLowerCase() !== address.toLowerCase()) {
        throw new Error(`Address mismatch: ${reqAddress} != ${address}`);
      }
      const data = typeof typedData === "string" ? JSON.parse(typedData) : typedData;
      const { domain, types, message: value, primaryType } = data;
      const typesNoDomain = { ...types };
      delete typesNoDomain.EIP712Domain;
      result = await wallet.signTypedData(domain, typesNoDomain, value);
      const recovered = verifyTypedData(domain, typesNoDomain, value, result);
      console.log(
        `[wallet] ${request.method} ok primaryType=${primaryType || "?"} recovered=${recovered}`
      );
      signedTyped = true;
    } else {
      throw new Error(`Unsupported method ${request.method}`);
    }
    await client.respond({
      topic,
      response: { id, jsonrpc: "2.0", result },
    });
  } catch (err) {
    console.error("[wallet] request failed", err);
    await client.respond({
      topic,
      response: {
        id,
        jsonrpc: "2.0",
        error: { code: 5000, message: String(err.message || err) },
      },
    });
  }

  const done = signedPersonal && (!expectTyped || signedTyped);
  if (done) {
    fs.writeFileSync(
      doneFile,
      JSON.stringify(
        {
          ok: true,
          address,
          sessionTopic,
          signedPersonal,
          signedTyped,
        },
        null,
        2
      )
    );
    console.log(`[wallet] DONE written to ${doneFile}`);
    // Give the dApp a moment to receive the last response, then exit.
    setTimeout(() => process.exit(0), 1500);
  }
});

const uri = await waitForUri(uriFile);
console.log(`[wallet] pairing with ${uri.slice(0, 48)}…`);
await client.core.pairing.pair({ uri });
await proposalHandled;

// Stay alive for session requests (exit from session_request handler).
setTimeout(() => {
  console.error("[wallet] timed out waiting for session requests");
  process.exit(1);
}, 90_000);
