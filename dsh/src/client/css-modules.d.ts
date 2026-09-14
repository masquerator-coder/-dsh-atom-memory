/** Ambient typing for CSS Modules so client TSX typechecks standalone. */
declare module '*.module.css' {
  const classes: Record<string, string>
  export default classes
}
