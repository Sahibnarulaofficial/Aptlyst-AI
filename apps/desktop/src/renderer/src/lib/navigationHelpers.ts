import { navigation } from './navigation'

export function getPageTitle(pathname: string) {
  const item = navigation.find((nav) => nav.path === pathname)

  return item?.title ?? 'Aptlyst AI'
}
