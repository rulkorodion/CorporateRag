let fallbackSequence = 0;

export function createClientId(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === "function") {
    return randomUUID.call(globalThis.crypto);
  }

  fallbackSequence += 1;
  return `exchange-${Date.now().toString(36)}-${fallbackSequence.toString(36)}`;
}
