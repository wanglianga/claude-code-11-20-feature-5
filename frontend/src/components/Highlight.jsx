// 按原文偏移区间高亮，区间来自后端 norm->orig 映射，特殊符号不会造成错位
export default function Highlight({ text, ranges }) {
  if (!ranges || ranges.length === 0) return <>{text}</>
  const merged = [...ranges]
    .map(([s, e]) => [s, e])
    .sort((a, b) => a[0] - b[0])
    .reduce((acc, [s, e]) => {
      if (acc.length && s <= acc[acc.length - 1][1]) {
        acc[acc.length - 1][1] = Math.max(acc[acc.length - 1][1], e)
      } else {
        acc.push([s, e])
      }
      return acc
    }, [])

  const parts = []
  let cursor = 0
  merged.forEach(([s, e], i) => {
    if (s > cursor) parts.push(<span key={`t${i}`}>{text.slice(cursor, s)}</span>)
    parts.push(<mark key={`m${i}`}>{text.slice(s, e)}</mark>)
    cursor = e
  })
  if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>)
  return <>{parts}</>
}
