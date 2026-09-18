// Pure composer-state contract. Kept separate so the single dynamic icon can be tested without a DOM.
export function composerAction(turnState, text){
  const hasText = String(text || '').trim().length > 0;
  if (turnState === 'running' && !hasText) return {mode: 'stop', disabled: false};
  return {mode: 'send', disabled: !hasText};
}
