type Props = {
  score: number;
  verdict: string;
};

export function ScoreRing({ score, verdict }: Props) {
  const radius = 78;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score / 100) * circumference;
  const [whole, fraction] = score.toFixed(1).split(".");

  return (
    <div className="score-ring-card">
      <svg viewBox="0 0 200 200" className="score-ring">
        <circle cx="100" cy="100" r={radius} className="score-ring-track" />
        <circle
          cx="100"
          cy="100"
          r={radius}
          className="score-ring-value"
          style={{ strokeDasharray: circumference, strokeDashoffset: offset }}
        />
      </svg>
      <div className="score-ring-content">
        <div className="score-ring-score">
          <span className="score-ring-whole">{whole}</span>
          <span className="score-ring-fraction">.{fraction}</span>
        </div>
        <span className="score-ring-label">из 100</span>
        <p>{verdict}</p>
      </div>
    </div>
  );
}
