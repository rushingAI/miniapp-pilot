export function foldAgentOutput(value, limit=200){
  const text = String(value == null ? '' : value);
  const characters = Array.from(text);
  const folded = characters.length > limit;
  return {
    folded,
    preview: folded ? `${characters.slice(0, limit).join('')}…` : text,
    text,
  };
}

function compactLegacyBlock(line){
  if (line.length < 1000 || !line.includes('"data"')) return line;
  try {
    const block = JSON.parse(line);
    const type = String(block && block.type || '').toLowerCase();
    if ((type === 'image' || type === 'audio') && typeof block.data === 'string'){
      return JSON.stringify({...block, data: `[binary omitted: ${block.data.length} chars]`});
    }
  } catch (_error) {
    // 不是独立 JSON content block；保持原始文本，避免误删普通工具结果。
  }
  return line;
}

export function compactLegacyToolResult(value){
  return String(value == null ? '' : value).split('\n').map(compactLegacyBlock).join('\n');
}

export function foldChatDelta(event, limit=200){
  const text = event && event.role === 'tool'
    ? compactLegacyToolResult(event.text)
    : event && event.text;
  return foldAgentOutput(text, limit);
}
