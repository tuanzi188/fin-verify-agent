import { useEffect, useState } from 'react'

/** 订阅一个 CSS 媒体查询。SSR/测试环境没有 matchMedia 时返回 false。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
    return window.matchMedia(query).matches
  })

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(query)
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches)
    setMatches(media.matches)
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [query])

  return matches
}
