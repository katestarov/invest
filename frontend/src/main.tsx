import React from "react";
import ReactDOM from "react-dom/client";
import AppScreen from "./AppScreen";
import "./styles/app.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppScreen />
  </React.StrictMode>,
);
