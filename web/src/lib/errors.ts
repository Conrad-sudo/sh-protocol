/**
 * Plain words for the contract errors the API reports. The API simulates every owner transaction
 * first and names the error that would stop it (app/api.py `_revert_reason`), e.g.
 * "This transaction would fail: SessionHandler_NotEnoughBalance()". The names come from the
 * SessionHandler and SpendingLimitModule ABIs.
 */
const PLAIN: Record<string, string> = {
  EnforcedPause: 'The wallet is already paused.',
  ExpectedPause: "The wallet isn't paused.",
  OwnableUnauthorizedAccount: "Only the wallet's owner can do this. Switch your browser wallet to the owner address.",
  SessionHandler_NotEnoughBalance: "The wallet doesn't hold that much.",
  SessionHandler_InvalidRecipient: 'Choose an address to send to.',
  SessionHandler_ExecutionFailed: "The receiving address didn't accept the transfer.",
  SessionHandler_TransferFailed: 'The transfer failed.',
  SafeERC20FailedOperation: "The token contract didn't allow the transfer.",
  SessionHandler_InvalidSessionKey: "That isn't a valid assistant key.",
  SessionHandler_InvalidMaxOpGasCost: 'The gas limit must be more than zero.',
  SessionHandler_MaxOpGasCostTooHigh: 'That gas limit is too high. Choose one under 1.2 million ETH.',
  SpendingLimitModule_InvalidDailyLimit: "The limit can't be negative.",
  SpendingLimitModule_InvalidWindowDuration: 'The period must be longer than zero.',
  SpendingLimitModule_TokenNotPriced: "Mitfah can't price this token, so it can't count toward your limit.",
  SpendingLimitModule_TooManyWatchedTokens: 'A wallet can count at most 32 tokens toward its limit.',
  SpendingLimitModule_TooManyTrustedSpenders: 'A wallet can trust at most 16 spenders.',
  SpendingLimitModule_InvalidTrustedSpender: "That isn't a valid spender address.",
  SpendingLimitModule_NotInstalled: "The spending limit isn't switched on for this wallet.",
  SpendingLimitModule_OracleNotSet: "Prices aren't available right now. Try again later.",
}

/** An API error message with any contract error in it replaced by plain words. */
export function explainError(message: string): string {
  for (const [, name] of message.matchAll(/\b(\w+)\(/g)) {
    if (Object.hasOwn(PLAIN, name)) return PLAIN[name]
  }
  if (/^That transaction reverted/.test(message)) return 'The transaction failed on the network, so nothing changed.'
  // The API's wording is written for API clients: it names the endpoints to call.
  if (/^Link your wallet address first/.test(message)) return "Your account isn't linked to a wallet yet."
  return message
}
