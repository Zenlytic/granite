import React from 'react';
import clsx from 'clsx';
import styles from './HomepageFeatures.module.css';

const FeatureList = [
  {
    title: 'Easy to Use',
    description: (
      <>
        Granite was designed from the ground up to be easily installed and
        used to start querying your metrics as soon as possible.
      </>
    ),
  },
  {
    title: 'Fast',
    description: (
      <>
        Granite resolves the correct query to run locally, then runs it
        directly against your warehouse. It's orders of magnitude faster than the Looker SDK.
      </>
    ),
  },
  {
    title: 'Reads LookML',
    description: (
      <>
        Granite uses your existing data model. Just point it to your LookML and
        you'll have access to your important metrics in python.
      </>
    ),
  },
];

function Feature({ Svg, title, description }) {
  return (
    <div style={{ paddingTop: '50px' }} className={clsx('col col--4')}>
      {/* <div className="text--center">
        <Svg className={styles.featureSvg} alt={title} />
      </div> */}
      <div className="text--center padding-horiz--md">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
