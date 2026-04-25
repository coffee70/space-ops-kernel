export function Component() {
  return (
    <div style={{ padding: "1.5rem" }}>
      <p style={{ fontSize: "0.75rem", letterSpacing: "0.18em", textTransform: "uppercase", opacity: 0.7 }}>
        Native Application
      </p>
      <h1 style={{ marginTop: "0.75rem" }}>{"{{display_name}}"}</h1>
      <p>{"{{description}}"}</p>
    </div>
  );
}
