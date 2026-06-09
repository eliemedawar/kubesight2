import { EMPTY_MESSAGES } from "../utils/authz.js";



export default function AccessDeniedPage({ message = EMPTY_MESSAGES.noAccess }) {

  return (

    <section className="card access-denied">

      <h2>Access restricted</h2>

      <p className="muted">{message}</p>

    </section>

  );

}



export function NoFeaturesPage() {

  return (

    <section className="card empty-state-card">

      <h2>Welcome to KubeSight</h2>

      <p>{EMPTY_MESSAGES.noFeatures}</p>

    </section>

  );

}


