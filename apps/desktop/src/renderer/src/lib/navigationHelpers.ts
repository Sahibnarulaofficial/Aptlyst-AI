import { navigation } from './navigation'

export function getCurrentNavigation(pathname: string) {
  return (
    navigation.find((item) => item.path === pathname) ?? {
      title: 'Aptlyst AI',
      description: 'AI Meeting Copilot'
    }
  )
}
